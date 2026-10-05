#!/usr/bin/env python3
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

HOME = Path.home()
HERMES_ROOT = HOME / ".hermes" / "hermes-agent"
DB = HOME / ".hermes" / "state.db"
PINS = HOME / ".hermes" / "jarvis-episode-pins.json"

sys.path.insert(0, str(HERMES_ROOT))
from tools.session_search_tool import session_search  # noqa: E402

STOP = {
    "que","qué","como","cómo","para","por","con","sin","del","las","los","una",
    "uno","unos","unas","esto","esta","este","eso","esa","ese","desde","hasta",
    "donde","dónde","sobre","entre","pero","más","muy","todo",
    "continua","continúa","continuar","continuamos","retoma","retomar","recupera",
    "recuperar","hicimos","hiciste","hicieron","ultimo","último","ultima","última",
    "estado","verificado","verificada","exactamente","exacto","historial","historia",
    "resume","resumas","resumen","trabajo","trabajamos","ahi","ahí","usalo","úsalo",
    "primero","real","the","and","for","with","from","continue","resume","previous",
    "last","history","work",
}

META_TOOLS = {
    "session_search","skill_view","skills_list","memory","memory_search","clarify",
    "todo","tool_search","tool_describe","tool_call",
}

MUTATION_TOOLS = {
    "patch","write_file","edit_file","apply_patch","str_replace_editor",
    "create_file","delete_file",
}

VERIFICATION_TOOLS = {
    "browser_console","browser_screenshot","browser_exec","browser_snapshot",
}

VERIFY_CMD = re.compile(
    r"(npm\s+(test|run\s+test|run\s+build)|pnpm\s+(test|run\s+test|run\s+build)|"
    r"yarn\s+(test|build)|pytest|cargo\s+test|dotnet\s+test|go\s+test|ctest|"
    r"make\s+(test|check))",
    re.I,
)

MUTATE_CMD = re.compile(
    r"(sed\s+-i|perl\s+-pi|git\s+(apply|commit|merge|rebase)|cp\s+|mv\s+)",
    re.I,
)

# No whitespace in the character class: a path embedded in a shell command must
# stop at the first space, otherwise "ls /a/b python -c" yields "/a/b python -c".
ABS_PATH = re.compile(r"/(?:home|mnt|opt|srv|tmp|var)/[A-Za-z0-9_./@+\-]+")
TOKEN = re.compile(r"[0-9A-Za-zÀ-ÖØ-öø-ÿ_+.#:/-]+")


def loads(value):
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def extract_terms(text):
    out = []
    seen = set()
    for raw in TOKEN.findall(text):
        clean = raw.strip(".,;:!?()[]{}'\"")
        low = clean.casefold()
        if len(low) < 3 or low in STOP or low in seen:
            continue
        seen.add(low)
        out.append(clean)

    def priority(term):
        special = (
            any(c.isupper() for c in term)
            or any(c.isdigit() for c in term)
            or "/" in term
            or "_" in term
        )
        return 0 if special else 1

    out.sort(key=priority)
    return out[:6]


def iter_calls(raw):
    data = loads(raw)
    if not isinstance(data, list):
        return
    for call in data:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") or {}
        name = fn.get("name") or call.get("name")
        raw_args = fn.get("arguments") if fn else call.get("arguments")
        args_obj = loads(raw_args)
        if not name:
            continue
        if isinstance(raw_args, str):
            args_text = raw_args
        else:
            args_text = json.dumps(raw_args, ensure_ascii=False, default=str)
        call_id = call.get("id") or call.get("tool_call_id") or ""
        yield str(name), args_text or "", args_obj, str(call_id)


def table_columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def session_meta(conn, sid):
    cols = table_columns(conn, "sessions")
    wanted = ["id","title","source","model","started_at","ended_at","parent_session_id"]
    selected = [x for x in wanted if x in cols]
    row = conn.execute(
        f"SELECT {', '.join(selected)} FROM sessions WHERE id = ?",
        (sid,),
    ).fetchone()
    return dict(row) if row else {}


def session_rows(conn, sid):
    cols = table_columns(conn, "messages")
    active = " AND active = 1" if "active" in cols else ""
    return conn.execute(
        f"SELECT id, role, content, tool_calls, timestamp "
        f"FROM messages WHERE session_id = ?{active} ORDER BY id",
        (sid,),
    ).fetchall()


FAILURE_TEXT = re.compile(
    r"(BLOCKED THIS TOOL CALL|Escape-drift detected|command not found|"
    r"No such file or directory|Permission denied|Traceback \(most recent call last\))",
    re.I,
)


def tool_outcomes(conn, sid):
    """Map tool_call_id -> True when the call actually succeeded.

    A tool call that errored produced no work, so it must not be counted as
    evidence of a work episode. Absent or unparseable results are treated as
    successful, so this can only ever demote calls we positively know failed.
    """
    cols = table_columns(conn, "messages")
    if "tool_call_id" not in cols:
        return {}

    active = " AND active = 1" if "active" in cols else ""
    outcomes = {}
    for row in conn.execute(
        f"SELECT tool_call_id, content FROM messages "
        f"WHERE session_id = ? AND role = 'tool'{active}",
        (sid,),
    ):
        call_id = row["tool_call_id"]
        if not call_id:
            continue
        content = row["content"] or ""
        data = loads(content)
        if isinstance(data, dict):
            if data.get("success") is False or "error" in data:
                outcomes[call_id] = False
                continue
            if data.get("success") is True:
                outcomes[call_id] = True
                continue
        outcomes[call_id] = not FAILURE_TEXT.search(content[:2000])
    return outcomes


def clean_path(value):
    if not isinstance(value, str):
        return None
    value = value.strip().rstrip("'\"),]} \n\t")
    return value if value.startswith("/") else None


def project_root(path):
    parts = Path(path).parts
    if len(parts) >= 5 and parts[1] == "home":
        return str(Path(*parts[:5]))
    if len(parts) >= 4:
        return str(Path(*parts[:4]))
    return path


def area_under(root, path):
    try:
        r = Path(root)
        p = Path(path)
        rel = p.relative_to(r)
    except Exception:
        return None
    if not rel.parts:
        return str(r)
    return str(r / rel.parts[0])


def walk_strings(obj):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key, value
            yield from walk_strings(value)
    elif isinstance(obj, list):
        for value in obj:
            yield None, value
            yield from walk_strings(value)


def paths_from_call(args_text, args_obj):
    found = []

    if isinstance(args_obj, dict):
        for key, value in walk_strings(args_obj):
            if not isinstance(value, str):
                continue
            if key in {"path", "workdir", "cwd", "file", "filename", "directory"}:
                p = clean_path(value)
                if p:
                    found.append((p, key))

    for p in ABS_PATH.findall(args_text):
        p = clean_path(p)
        if p:
            found.append((p, "embedded"))

    out = []
    seen = set()
    for item in found:
        if item[0] in seen:
            continue
        seen.add(item[0])
        out.append(item)
    return out


def evidence(conn, sid):
    direct = []
    meta = []
    mutations = []
    verifies = []
    inspections = []
    visible = []

    root_scores = collections.Counter()
    area_scores = collections.Counter()
    path_scores = collections.Counter()
    path_last_id = {}

    outcomes = tool_outcomes(conn, sid)
    failed_calls = 0

    for row in session_rows(conn, sid):
        role = row["role"]
        content = row["content"] or ""

        if role in ("user", "assistant"):
            visible.append({
                "id": row["id"],
                "role": role,
                "content": content,
            })

        if role != "assistant":
            continue

        for name, args_text, args_obj, call_id in iter_calls(row["tool_calls"]):
            rec = {
                "message_id": row["id"],
                "tool": name,
                "args": " ".join(args_text.split())[:600],
            }

            if name in META_TOOLS:
                meta.append(rec)
                continue

            lname = name.casefold()

            # A call that failed changed nothing: it is neither work nor
            # verification, and its paths are not evidence of where work
            # happened. Counting them lets a session full of blocked or
            # malformed calls masquerade as a WORK_EPISODE.
            if call_id and outcomes.get(call_id) is False:
                failed_calls += 1
                continue

            if name in MUTATION_TOOLS or (
                lname == "terminal" and MUTATE_CMD.search(args_text)
            ):
                kind = "mutation"
                weight = 8
                mutations.append(rec)
            elif name in VERIFICATION_TOOLS or (
                lname == "terminal" and VERIFY_CMD.search(args_text)
            ):
                kind = "verification"
                weight = 6
                verifies.append(rec)
            else:
                kind = "inspection"
                weight = 2
                inspections.append(rec)

            direct.append({**rec, "kind": kind})

            for path, source in paths_from_call(args_text, args_obj):
                po = Path(path)
                area_path = str(po.parent) if po.suffix else str(po)
                root = project_root(path)

                root_scores[root] += weight
                area = area_under(root, area_path)
                if area:
                    area_scores[area] += weight

                extra = 2 if source in {"path", "workdir", "cwd", "directory"} else 0
                path_scores[path] += weight + extra
                path_last_id[path] = row["id"]

    dominant_root = root_scores.most_common(1)[0][0] if root_scores else None

    action_area_scores = collections.Counter()
    if dominant_root:
        for area, score in area_scores.items():
            if area == dominant_root or area.startswith(dominant_root + "/"):
                action_area_scores[area] += score

    action_root = (
        action_area_scores.most_common(1)[0][0]
        if action_area_scores
        else dominant_root
    )

    support_roots = [
        area
        for area, _ in action_area_scores.most_common(4)
        if area != action_root
    ]

    ranked_paths = sorted(
        path_scores,
        key=lambda p: (path_scores[p], path_last_id.get(p, 0)),
        reverse=True,
    )

    key_paths = []
    for p in ranked_paths:
        if dominant_root and not (p == dominant_root or p.startswith(dominant_root + "/")):
            continue
        if p not in key_paths:
            key_paths.append(p)
        if len(key_paths) >= 8:
            break

    if mutations and verifies:
        classification, provenance = "VERIFIED_WORK_EPISODE", 120
    elif mutations:
        classification, provenance = "WORK_EPISODE", 90
    elif verifies:
        classification, provenance = "VERIFY_ONLY_EPISODE", 55
    elif meta and not direct:
        classification, provenance = "RECALL_ONLY_EPISODE", -50
    elif meta:
        classification, provenance = "RECALL_OR_INSPECTION_EPISODE", -10
    else:
        classification, provenance = "INSPECTION_ONLY_EPISODE", 20

    final = visible[-1] if visible else None
    unresolved = final if final and final["role"] == "user" else None

    return {
        "classification": classification,
        "provenance": provenance,
        "direct_tool_calls": len(direct),
        "meta_tool_calls": len(meta),
        "mutation_calls": len(mutations),
        "verification_calls": len(verifies),
        "inspection_calls": len(inspections),
        "failed_calls": failed_calls,
        "dominant_root": dominant_root,
        "action_root": action_root,
        "support_roots": support_roots,
        "key_paths": key_paths,
        "unresolved_user_feedback": unresolved,
        "last_messages": visible[-6:],
        "mutations": mutations[-4:],
        "verifications": verifies[-4:],
    }


def load_pins():
    """Read topic->root anchors from jarvis-episode-pins.json.

    A pin states that a topic definitively belongs to a project, for cases
    where the session corpus cannot express it: the real work sits under a
    title that never mentions the topic, while failed retries do.
    """
    if not PINS.exists():
        return []
    data = loads(PINS.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("pins")
    return data if isinstance(data, list) else []


def match_pin(request, pins):
    """Return the pin with the most matching terms, or None."""
    low = request.casefold()
    best = None
    best_hits = 0
    for pin in pins:
        if not isinstance(pin, dict):
            continue
        terms = [str(t).casefold() for t in (pin.get("match") or [])]
        if not terms:
            continue
        hits = sum(1 for t in terms if t in low)
        required = pin.get("require_terms", 2)
        if hits >= min(required, len(terms)) and hits > best_hits:
            best, best_hits = pin, hits
    return best


def pin_roots(pin):
    roots = [pin.get("action_root")] + list(pin.get("support_roots") or [])
    return [str(r).rstrip("/") for r in roots if r]


def under_any(path, roots):
    if not path:
        return False
    path = str(path).rstrip("/")
    return any(path == r or path.startswith(r + "/") for r in roots)


def search_candidates(request, current_sid):
    terms = extract_terms(request)
    if not terms:
        return terms, {}

    strict = " ".join(terms[:4])
    broad = " OR ".join(terms[:5])

    # Each pass is (query, roles, weight, sort, limit).
    #
    # The "oldest" passes exist because failed recall attempts pile up as recent
    # sessions that mention the topic by name, while the session that did the
    # original work is older and often titled something else entirely. Sorting
    # only by "newest" fills the window with those retries and the genuine
    # episode never becomes a candidate at all — no amount of rescoring can
    # rank a session that was never retrieved.
    passes = [
        (strict, "user", 12, "newest", 30),
        (strict, "user,assistant", 8, "newest", 30),
        (broad, "user,assistant", 4, "newest", 30),
        (strict, "user,assistant", 8, "oldest", 30),
        (broad, "user,assistant", 4, "oldest", 30),
    ]

    candidates = {}

    for query, roles, weight, sort, limit in passes:
        raw = session_search(
            query=query,
            role_filter=roles,
            limit=limit,
            sort=sort,
            current_session_id=current_sid,
        )
        data = json.loads(raw)
        if not data.get("success"):
            continue

        for hit in data.get("results", []):
            sid = hit.get("session_id")
            if not sid or sid == current_sid:
                continue

            c = candidates.setdefault(
                sid,
                {
                    "session_id": sid,
                    "search_score": 0,
                    "title": hit.get("title") or "",
                },
            )
            c["search_score"] += weight

    return terms, candidates


def compact(text, limit=650):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("request")
    ap.add_argument("--handoff", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not DB.exists():
        raise SystemExit(f"Missing DB: {DB}")

    current_sid = os.environ.get("HERMES_SESSION_ID")
    terms, candidates = search_candidates(args.request, current_sid)

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    ranked = []
    try:
        qterms = [t.casefold() for t in terms]
        for sid, c in candidates.items():
            meta = session_meta(conn, sid)
            ev = evidence(conn, sid)
            title = meta.get("title") or c.get("title") or ""
            title_low = title.casefold()
            title_hits = sum(1 for t in qterms if t in title_low)

            # Volume of successful work, not just its presence. Without this,
            # a session with one lucky mutation scores the same as one with
            # seventy, because provenance is a flat bonus and direct_tool_calls
            # is capped at 10.
            work_volume = (
                min(ev["mutation_calls"], 30)
                + min(ev["verification_calls"], 15) * 2
            )

            score = (
                ev["provenance"]
                + c["search_score"]
                + title_hits * 5
                + min(ev["direct_tool_calls"], 10)
                - min(ev["meta_tool_calls"] * 3, 18)
                + work_volume
            )

            # Structural gate, deliberately not arithmetic: any episode that
            # genuinely did work outranks any that did not, regardless of how
            # well its title matches the query. Titles are what failed recall
            # attempts are good at; sustained successful mutations are not.
            genuine = (
                ev["classification"] in ("VERIFIED_WORK_EPISODE", "WORK_EPISODE")
                and (ev["mutation_calls"] + ev["verification_calls"]) >= 4
            )

            ranked.append({
                "session_id": sid,
                "title": title,
                "started_at": meta.get("started_at"),
                "score": score,
                "work_volume": work_volume,
                "genuine": genuine,
                **ev,
            })
    finally:
        conn.close()

    ranked.sort(
        key=lambda x: (x["genuine"], x["score"], x.get("started_at") or 0),
        reverse=True,
    )

    # A matching pin narrows the candidate set to the anchored project rather
    # than overriding the handoff, so the winning episode still contributes its
    # key paths and unresolved user feedback.
    pin = match_pin(args.request, load_pins())
    pinned_fallback = False
    if pin:
        roots = pin_roots(pin)
        inside = [
            r for r in ranked
            if under_any(r.get("dominant_root"), roots)
            or under_any(r.get("action_root"), roots)
        ]
        if inside:
            ranked = inside
        else:
            pinned_fallback = True

    selected = ranked[0] if ranked else None

    if pin and (pinned_fallback or not selected):
        # No episode under the anchored project: emit the pin itself rather
        # than hand back a confidently wrong root.
        selected = {
            "session_id": "PIN",
            "title": pin.get("note") or "pinned anchor",
            "classification": "PINNED_ANCHOR",
            "dominant_root": pin.get("action_root"),
            "action_root": pin.get("action_root"),
            "support_roots": list(pin.get("support_roots") or []),
            "key_paths": list(pin.get("key_paths") or []),
            "unresolved_user_feedback": None,
        }

    if args.json:
        print(json.dumps(
            {
                "request": args.request,
                "derived_terms": terms,
                "selected": selected,
                "candidates": ranked[:8],
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ))
        return

    print("JARVIS_HANDOFF_V2")

    if not selected:
        print("FOUND=0")
        print("NEXT=NO_GENUINE_EPISODE_FOUND")
        return

    print("FOUND=1")
    print(f"SESSION_ID={selected['session_id']}")
    print(f"TITLE={selected.get('title') or ''}")
    print(f"CLASSIFICATION={selected.get('classification') or ''}")
    print(f"DOMINANT_ROOT={selected.get('dominant_root') or ''}")
    print(f"ACTION_ROOT={selected.get('action_root') or ''}")

    support = selected.get("support_roots") or []
    if support:
        print("SUPPORT_ROOTS=" + ";".join(support))

    for i, p in enumerate(selected.get("key_paths") or [], 1):
        print(f"KEY_PATH_{i}={p}")

    unresolved = selected.get("unresolved_user_feedback")
    if unresolved:
        print("UNRESOLVED_USER_FEEDBACK=" + compact(unresolved.get("content"), 700))
        print(
            "NEXT_OBJECTIVE=Reproduce the unresolved user-visible failure using "
            "ACTION_ROOT and KEY_PATHS, fix the actual cause, and verify the "
            "resulting behavior instead of trusting previous tests alone."
        )
    else:
        print(
            "NEXT_OBJECTIVE=Inspect current live state only inside ACTION_ROOT/"
            "SUPPORT_ROOTS, continue the last genuine work, and verify the outcome."
        )

    print("NEXT=LIVE_STATE_THEN_ACT_THEN_VERIFY")
    print("SCOPE_LOCK=ACTION_ROOT,SUPPORT_ROOTS,KEY_PATHS")
    print(
        "FORBIDDEN_UNTIL_FIRST_ACTION="
        "tool_search,tool_describe,tool_call,session_search,skills_search,"
        "broad_home_scan,broad_project_find,unrelated_project_subtree"
    )


if __name__ == "__main__":
    main()
