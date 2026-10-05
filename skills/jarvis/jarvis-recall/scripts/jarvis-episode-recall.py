#!/usr/bin/env python3
"""Deterministic recall for "continue previous work" requests.

Answers one question: which PROJECT does this request continue? The answer
becomes a scope lock (see the jarvis-scope-guard plugin), so a wrong project
is worse than no project. Every step below prefers abstaining (FOUND=0) over
guessing.

Decision order:
  1. pin            a configured topic -> project anchor
  2. anchor         absolute paths written in the request
  3. name           a discriminative request term names a project
  4. current        the current session's own recent work; only a specific
                    topic it never discussed moves the decision elsewhere
  5. content        the work done in the past turns that discussed the
                    request's specific terms (identifiers, proper names, rare words)
Anything weaker abstains and asks the user which project.

Evidence comes straight from Hermes' state.db (no Hermes imports), and only
from messages older than `as_of`, so the same code can be evaluated honestly
against past requests.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import os
import re
import sqlite3
import time
from pathlib import Path

HOME = Path.home()
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(HOME / ".hermes"))).expanduser()
DB = HERMES_HOME / "state.db"
PINS = HERMES_HOME / "jarvis-episode-pins.json"

# --- tunables ----------------------------------------------------------------
# Kept few and structural on purpose; each one is a statement about evidence,
# not a fitted coefficient.

MUTATION_WEIGHT = 8
VERIFICATION_WEIGHT = 6
INSPECTION_WEIGHT = 2

DISCRIMINATIVE_DF = 0.2   # a term in more than 20% of sessions says nothing...
DF_FLOOR = 3               # ...but in a tiny corpus, a handful of sessions is fine
RARE_SHARE = 0.02          # rare word: in at most DF_FLOOR sessions and this share
CLEAR_MARGIN = 1.5         # winner must beat the runner-up by this ratio
ANCHOR_SHARE = 0.6         # share of request paths that must agree on a root
CURRENT_WINDOW = 30        # recent work calls that define "where we are now"
LONG_REQUEST = 800         # longer than this is treated as pasted material
HEAD_CHARS = 300           # ...and only its head...
TAIL_LINE = 160            # ...and a short last line typed after the paste are intent

MARKERS = (
    ".git", "package.json", "pyproject.toml", "setup.py", "pom.xml",
    "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
    "Cargo.toml", "go.mod", "composer.json", "Gemfile", "CMakeLists.txt",
    "deno.json", "mix.exs",
)

# Directories that hold many unrelated things. A project is one level below.
BUCKET_NAMES = {
    "documents", "documentos", "downloads", "descargas", "desktop", "escritorio",
    "projects", "proyectos", "repos", "src", "code", "dev", "workspace", "work",
}

# Path segments below which nothing is project evidence of its own.
NOISE_SEGMENTS = {"node_modules", ".venv", "venv", "site-packages", "__pycache__", ".git", "dist", "build"}

IGNORED_HOME_DIRS = {
    ".cache", ".local", ".pyenv", ".npm", ".cargo", ".rustup", ".gradle", ".m2",
    ".android", ".mozilla", ".nvm", ".bun", ".deno", ".hermes", ".claude",
    ".Trash", "Android",
}

STOP = {
    "que", "qué", "como", "cómo", "para", "por", "con", "sin", "del", "las", "los",
    "una", "uno", "unos", "unas", "esto", "esta", "este", "eso", "esa", "ese",
    "desde", "hasta", "donde", "dónde", "sobre", "entre", "pero", "más", "muy",
    "todo", "toda", "todos", "tema", "cosa", "cosas", "algo", "aquí", "aqui",
    "continua", "continúa", "continuar", "continuamos", "continue", "retoma",
    "retomar", "retomamos", "recupera", "recuperar", "hicimos", "hiciste",
    "hicieron", "teníamos", "teniamos", "ultimo", "último", "ultima", "última",
    "estado", "verificado", "verificada", "exactamente", "exacto", "historial",
    "historia", "resume", "resumas", "resumen", "trabajo", "trabajamos", "ahi",
    "ahí", "usalo", "úsalo", "primero", "real", "final", "momento", "ningun",
    "ningún", "pares", "the", "and", "for", "with", "from", "resume", "previous",
    "last", "history", "work", "where", "left", "off", "this", "that", "lo",
}

META_TOOLS = {
    "session_search", "skill_view", "skills_list", "memory", "memory_search",
    "clarify", "todo", "tool_search", "tool_describe", "tool_call",
}
MUTATION_TOOLS = {
    "patch", "write_file", "edit_file", "apply_patch", "str_replace_editor",
    "create_file", "delete_file",
}
VERIFICATION_TOOLS = {"browser_console", "browser_screenshot", "browser_exec", "browser_snapshot"}

VERIFY_CMD = re.compile(
    r"(npm\s+(test|run\s+test|run\s+build)|pnpm\s+(test|run\s+test|run\s+build)|"
    r"yarn\s+(test|build)|pytest|cargo\s+(test|build)|dotnet\s+test|go\s+test|ctest|"
    r"make\s+(test|check)|mvn\s+\S*\s*(test|package|install|verify)|gradlew?\s+\S*(test|build|assemble))",
    re.I,
)
MUTATE_CMD = re.compile(r"(sed\s+-i|perl\s+-pi|git\s+(apply|commit|merge|rebase)|\bcp\s+|\bmv\s+)", re.I)

FAILURE_TEXT = re.compile(
    r"(BLOCKED THIS TOOL CALL|command not found|No such file or directory|"
    r"Permission denied|Traceback \(most recent call last\))",
    re.I,
)

# Quoted paths may contain spaces; bare ones stop at the first space.
# Any absolute path with at least two segments; the resolver decides which
# ones belong to the user's projects. URLs are excluded by the lookbehind.
# Inside quotes a segment may hold spaces, but must start with a name character:
# "/repo/ some prose" is prose after a path, not a directory called " some...".
QUOTED_PATH = re.compile(
    r"""(["'`])(/[\w.@+\-][^/"'`\n]{0,254}(?:/[\w.@+\-][^/"'`\n]{0,254})+)\1"""
)
BARE_PATH = re.compile(r"(?<![\w.:/])/[\w.@+\-]+(?:/[\w.@+\-]+)+")
TOKEN = re.compile(r"[0-9A-Za-zÀ-ÖØ-öø-ÿ_+.#-]+")
CODE_FENCE = re.compile(r"```.*?```", re.S)
PATH_CHUNK = re.compile(r"\S*/\S*")
# Claude Code's status line ("Opus 5 (1M context) │ billing-api ███░░ 31%")
# shows the basename of the directory the pasted session was working in.
STATUSLINE = re.compile(r"│\s*([\w.@+\-]+)\s+[█░▓]")


# --- text helpers --------------------------------------------------------------


def loads(value):
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def clean_path(value: str) -> str:
    return value.strip().rstrip("'\"`),]};:.\n\t ")


def paths_in_text(text: str) -> list[str]:
    """Absolute paths in free text, quoted ones kept whole even with spaces."""
    found: list[str] = []
    spans = []
    for m in QUOTED_PATH.finditer(text or ""):
        found.append(clean_path(m.group(2)))
        spans.append(m.span())
    for m in BARE_PATH.finditer(text or ""):
        if any(a <= m.start() < b for a, b in spans):
            continue
        found.append(clean_path(m.group(0)))
    out = []
    for p in found:
        if p and p not in out:
            out.append(p)
    return out


CODE_SYMBOLS = set("{}[]()=;:'\"<>`|\\")
CODE_DENSITY = 0.10        # prose stays well under 10% of these symbols; code does not


def looks_like_code(text: str) -> bool:
    chars = [c for c in text if not c.isspace()]
    return bool(chars) and sum(c in CODE_SYMBOLS for c in chars) / len(chars) >= CODE_DENSITY


TUI_GLYPHS = "⎿●❯⏵✻※│─╭╰✶✳·"
DOC_MARKUP = "#[{<`|>\"'*-"


def typed(line: str) -> bool:
    """A short line the user typed, not one copied from a terminal or a document."""
    s = line.strip()
    return 0 < len(s) <= TAIL_LINE and s[0] not in TUI_GLYPHS + DOC_MARKUP and not looks_like_code(s)


def intent_text(request: str) -> str:
    """The part of a request that expresses what the user wants.

    Long messages are mostly pasted material (logs, specs, code): their body
    names everything and therefore nothing. A head that is code is pasted
    too. Intent sits at the head or the last line when it is short
    ("continue" typed after a pasted log). When the message opens with a
    typed line ("Continúa con lo pendiente", "Soluciona esto:") that line is
    the whole head: what follows it is the material, not the request.
    Transcript lines ("⎿ Interrupted") and document markup are never typed.
    """
    text = CODE_FENCE.sub(" ", request or "")
    if len(text) > LONG_REQUEST:
        first = text.lstrip("\n").split("\n", 1)[0]
        head = first if typed(first) else text[:HEAD_CHARS]
        last = text.rstrip().rsplit("\n", 1)[-1]
        text = ("" if looks_like_code(head) else head) + ("\n" + last if typed(last) else "")
    return text


NEGATORS = {"no", "sin", "ni", "nunca", "jamás", "evita", "excepto", "salvo",
             "not", "without", "never", "dont", "avoid", "except"}
CLAUSE = re.compile(r"[.;,:\n()!?]+")


def negated_terms(text: str) -> set[str]:
    """Terms that only ever appear after a negation in their clause.

    "no toques billing-api" mentions a project to exclude it; counting that
    mention as an anchor locks the agent into the one place it must avoid.
    """
    negated, affirmed = set(), set()
    for clause in CLAUSE.split(text or ""):
        seen_negator = False
        for raw in TOKEN.findall(clause):
            low = raw.strip(".,;:!?()[]{}'\"#+-_").casefold()
            if low in NEGATORS:
                seen_negator = True
                continue
            (negated if seen_negator else affirmed).add(low)
    return negated - affirmed


SENTENCE = re.compile(r"[.!?\n]+\s+|\n+")
PERMISSIONS = re.compile(r"^[dlcbps-]?[r-][w-][xsS-][r-]?[w-]?[xsS-]?[r-]?[w-]?[xtT-]?$")
UNIT = re.compile(r"^\d+(?:[.,]\d+)?(?:am|pm|ms|s|m|h|d|k|kb|mb|gb|tb|px|x)$", re.I)


def named_terms(text: str) -> set[str]:
    """Terms whose FORM marks them as specific: identifiers and proper names.

    WD10, billing-api, mask.py, ComfyUI, VM, or a capitalized word in the
    middle of a sentence ("el portfolio de Arch"). A capital at the start of a
    line or sentence says nothing: UI labels and sentences all have one.
    """
    out = set()
    for sentence in SENTENCE.split(text or ""):
        for i, raw in enumerate(TOKEN.findall(sentence)):
            word = raw.strip(".,;:!?()[]{}'\"#+-_")
            if len(word) < 2 or UNIT.match(word):
                continue
            if "-" in word and len(word) >= 6 and PERMISSIONS.match(word):
                continue
            # Inner capitals mark ComfyUI or an acronym (VM, CSS); a long
            # all-caps word is a shouted label or key name (INTRO, IMPORTANT).
            shouted = word.isupper() and len(word) > 4
            identifier = (
                (any(c.isdigit() for c in word) and any(c.isalpha() for c in word))
                or re.search(r"\w[-_.]\w", word) is not None
                or (any(c.isupper() for c in word[1:]) and not shouted)
            )
            proper = word[0].isupper() and i > 0 and not shouted
            if identifier or proper:
                out.add(word.casefold())
    return out


def extract_terms(text: str) -> list[str]:
    """Topic words. Paths are left to the anchor step: split into words they
    yield "usr", "share" or "venv", which name nothing the user asked for."""
    out, seen = [], set()
    for raw in TOKEN.findall(PATH_CHUNK.sub(" ", text)):
        clean = raw.strip(".,;:!?()[]{}'\"#+-_")
        low = clean.casefold()
        if len(low) < 3 or low in STOP or low.isdigit() or UNIT.match(low) or low in seen:
            continue
        seen.add(low)
        out.append(clean)
    return out


def name_tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[-_.\s]+", name.casefold()) if t}


def name_match(term: str, project: str) -> float:
    """1 for the project's name or a whole token of it, 0.5 for a prefix."""
    t = term.casefold()
    base = Path(project).name.casefold()
    if t == base:
        return 1.0
    tokens = name_tokens(base)
    if t in tokens:
        return 1.0
    if len(t) >= 4 and any(tok.startswith(t) for tok in tokens):
        return 0.5
    return 0.0


# --- project roots -------------------------------------------------------------


class RootResolver:
    """Maps any path to the project that owns it.

    Order: nearest ancestor with .git, then nearest ancestor with a build
    marker, then the directory just below a bucket (Documents, ~/projects...).
    Markers come from disk AND from paths seen in past sessions, so a repo
    deleted since still resolves to its old root.
    """

    def __init__(self, home: Path, hermes_home: Path | None = None):
        self.home = Path(home)
        self.hermes_home = Path(hermes_home) if hermes_home else self.home / ".hermes"
        self.observed_git: set[str] = set()
        self.observed_marker: set[str] = set()
        self._cache: dict[str, str | None] = {}
        self._container: dict[str, bool] = {}

    def observe(self, path: str) -> None:
        p = Path(path)
        if ".git" in p.parts:
            idx = p.parts.index(".git")
            self.observed_git.add(str(Path(*p.parts[:idx])))
        elif p.name in MARKERS:
            self.observed_marker.add(str(p.parent))

    def _ignored(self, p: Path) -> bool:
        if p == self.home or self.home not in p.parents and not str(p).startswith(("/mnt/", "/srv/", "/opt/")):
            return True
        if p == self.hermes_home or self.hermes_home in p.parents:
            return True
        if self.home in p.parents:
            first = p.relative_to(self.home).parts[0]
            if first in IGNORED_HOME_DIRS:
                return True
        return False

    def _has_git(self, d: Path) -> bool:
        return str(d) in self.observed_git or (d / ".git").exists()

    def _has_marker(self, d: Path) -> bool:
        if str(d) in self.observed_marker or str(d) in self.observed_git:
            return True
        return any((d / m).exists() for m in MARKERS)

    def _is_container(self, d: Path) -> bool:
        key = str(d)
        if key in self._container:
            return self._container[key]
        result = d == self.home or d.name.casefold() in BUCKET_NAMES or d.parent in (Path("/mnt"), Path("/srv"))
        if not result:
            children = 0
            prefix = key + "/"
            observed = {r for r in self.observed_git | self.observed_marker if r.startswith(prefix)}
            children = len({Path(r).relative_to(d).parts[0] for r in observed})
            if children < 3 and d.is_dir():
                try:
                    children = max(children, sum(
                        1 for c in d.iterdir()
                        if c.is_dir() and any((c / m).exists() for m in (".git", "package.json", "pyproject.toml"))
                    ))
                except OSError:
                    pass
            result = children >= 3
        self._container[key] = result
        return result

    def root(self, path: str) -> str | None:
        if not path or not os.path.isabs(path):
            return None
        if path not in self._cache:
            try:
                self._cache[path] = self._resolve(path)
            except (OSError, ValueError):
                self._cache[path] = None
        return self._cache[path]

    def _resolve(self, path: str) -> str | None:
        p = Path(os.path.normpath(path))
        parts = p.parts
        for i, seg in enumerate(parts):
            if seg in NOISE_SEGMENTS:
                p = Path(*parts[:i])
                break
        result = None
        if not self._ignored(p):
            start = p.parent if (p.suffix and not p.is_dir()) or p.is_file() else p
            chain = [start, *start.parents]
            chain = [d for d in chain if d != self.home and (self.home in d.parents or str(d).startswith(("/mnt/", "/srv/", "/opt/")))]
            result = next((str(d) for d in chain if self._has_git(d)), None)
            if result is None:
                result = next((str(d) for d in chain if self._has_marker(d)), None)
            if result is None:
                for d in chain:
                    if self._is_container(d.parent) or d.parent == self.home:
                        result = str(d)
                        break
        return result


# --- evidence from state.db ------------------------------------------------------


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
        if not name:
            continue
        args_obj = loads(raw_args)
        args_text = raw_args if isinstance(raw_args, str) else json.dumps(raw_args, ensure_ascii=False, default=str)
        yield str(name), args_text or "", args_obj, str(call.get("id") or call.get("tool_call_id") or "")


def call_paths(args_text: str, args_obj) -> list[tuple[str, bool]]:
    """(path, explicit) pairs; explicit means a path/workdir argument."""
    out: list[tuple[str, bool]] = []
    if isinstance(args_obj, dict):
        stack = [args_obj]
        while stack:
            cur = stack.pop()
            for key, value in cur.items():
                if isinstance(value, dict):
                    stack.append(value)
                elif isinstance(value, str) and key in {"path", "workdir", "cwd", "file", "file_path", "filename", "directory"}:
                    if os.path.isabs(value):
                        out.append((clean_path(value), True))
                elif isinstance(value, str):
                    out.extend((p, False) for p in paths_in_text(value))
    else:
        out.extend((p, False) for p in paths_in_text(args_text))
    seen, unique = set(), []
    for p, explicit in out:
        if p not in seen:
            seen.add(p)
            unique.append((p, explicit))
    return unique


def classify(name: str, args_text: str) -> tuple[str, int]:
    lname = name.casefold()
    if name in MUTATION_TOOLS or (lname == "terminal" and MUTATE_CMD.search(args_text)):
        return "mutation", MUTATION_WEIGHT
    if name in VERIFICATION_TOOLS or (lname == "terminal" and VERIFY_CMD.search(args_text)):
        return "verification", VERIFICATION_WEIGHT
    return "inspection", INSPECTION_WEIGHT


class Corpus:
    """All sessions older than as_of, reduced to project evidence."""

    def __init__(self, conn: sqlite3.Connection, resolver: RootResolver, as_of: float):
        self.conn = conn
        self.resolver = resolver
        self.as_of = as_of
        self.sessions: dict[str, dict] = {}
        self.parents: dict[str, str] = {}
        self._hits: dict[str, list[tuple[str, int]]] = {}
        self._load()

    def _load(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(messages)")}
        active = " AND active = 1" if "active" in cols else ""
        scols = {r[1] for r in self.conn.execute("PRAGMA table_info(sessions)")}
        title_col = "title" if "title" in scols else "''"
        if "parent_session_id" in scols:
            # Metadata only: a session continued after compression may start
            # after as_of, but which conversation it continues is no leak.
            self.parents = dict(self.conn.execute(
                "SELECT id, parent_session_id FROM sessions WHERE parent_session_id IS NOT NULL"))
        for sid, title, started in self.conn.execute(
            f"SELECT id, {title_col}, started_at FROM sessions WHERE started_at < ? ORDER BY started_at",
            (self.as_of,),
        ):
            self.sessions[sid] = {
                "id": sid, "title": title or "", "started_at": started,
                "calls": [], "visible": [], "last_ts": started, "turns": [0],
            }

        failed: set[str] = set()
        for call_id, content in self.conn.execute(
            f"SELECT tool_call_id, content FROM messages WHERE role = 'tool' AND timestamp < ?{active}",
            (self.as_of,),
        ):
            if not call_id:
                continue
            data = loads(content)
            if isinstance(data, dict) and (data.get("success") is False or data.get("error")):
                failed.add(call_id)
            elif not isinstance(data, dict) and FAILURE_TEXT.search((content or "")[:2000]):
                failed.add(call_id)

        raw_calls = []
        for mid, sid, role, content, tool_calls, ts in self.conn.execute(
            f"SELECT id, session_id, role, content, tool_calls, timestamp FROM messages "
            f"WHERE timestamp < ?{active} ORDER BY id",
            (self.as_of,),
        ):
            s = self.sessions.get(sid)
            if s is None:
                continue
            s["last_ts"] = max(s["last_ts"] or 0, ts or 0)
            if role in ("user", "assistant") and content:
                s["visible"].append({"id": mid, "role": role, "content": content})
            if role == "user":
                s["turns"].append(mid)
            if role != "assistant" or not tool_calls:
                continue
            for name, args_text, args_obj, call_id in iter_calls(tool_calls):
                if name in META_TOOLS or (call_id and call_id in failed):
                    continue
                paths = call_paths(args_text, args_obj)
                for p, _ in paths:
                    self.resolver.observe(p)
                raw_calls.append((s, mid, name, args_text, paths))

        for s, mid, name, args_text, paths in raw_calls:
            kind, weight = classify(name, args_text)
            for p, explicit in paths:
                root = self.resolver.root(p)
                if root:
                    s["calls"].append({
                        "id": mid, "kind": kind, "root": root, "path": p,
                        "weight": weight + (2 if explicit else 0),
                    })

        for s in self.sessions.values():
            weights = collections.Counter()
            per_turn: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
            for c in s["calls"]:
                weights[c["root"]] += c["weight"]
                per_turn[self._turn_of(s, c["id"])][c["root"]] += c["weight"]
            s["weights"] = weights
            s["total"] = sum(weights.values())
            s["turn_weights"] = per_turn

    @staticmethod
    def _turn_of(session: dict, message_id: int) -> int:
        """Start id of the user turn that contains this message."""
        starts = session["turns"]
        lo, hi = 0, len(starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if starts[mid] <= message_id:
                lo = mid
            else:
                hi = mid - 1
        return starts[lo]

    def hits(self, term: str) -> list[tuple[str, int]]:
        """(session, message) pairs where the user or assistant wrote the term.

        Tool output is excluded: a grep that prints a word is not a
        conversation about it.
        """
        if term not in self._hits:
            phrase = '"' + term.replace('"', " ") + '"'
            try:
                rows = self.conn.execute(
                    "SELECT m.session_id, m.id FROM messages_fts f JOIN messages m ON m.id = f.rowid "
                    "WHERE messages_fts MATCH ? AND m.timestamp < ? AND m.role IN ('user', 'assistant')",
                    (phrase, self.as_of),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = self.conn.execute(
                    "SELECT session_id, id FROM messages WHERE timestamp < ? "
                    "AND role IN ('user', 'assistant') AND content LIKE ?",
                    (self.as_of, f"%{term}%"),
                ).fetchall()
            self._hits[term] = [(s, m) for s, m in rows if s in self.sessions]
        return self._hits[term]

    def document_frequency(self, term: str) -> set[str]:
        return {s for s, _ in self.hits(term)}

    def attributed_work(self, term: str) -> collections.Counter:
        """Project weights of the work done in the turns that mention the term."""
        out = collections.Counter()
        seen = set()
        for sid, mid in self.hits(term):
            session = self.sessions[sid]
            turn = self._turn_of(session, mid)
            if (sid, turn) in seen:
                continue
            seen.add((sid, turn))
            out.update(session["turn_weights"].get(turn, {}))
        return out

    def projects(self) -> set[str]:
        out = set()
        for s in self.sessions.values():
            out.update(s["weights"])
        return out


# --- decision ------------------------------------------------------------------


def _result(found, mode, root=None, session=None, corpus=None, candidates=(), reason=""):
    out = {
        "found": found, "mode": mode, "action_root": root, "reason": reason,
        "candidates": list(candidates), "session_id": None, "title": "",
        "support_roots": [], "key_paths": [], "unresolved_user_feedback": None,
    }
    if found and session is not None:
        out["session_id"] = session["id"]
        out["title"] = session["title"]
        total = session["total"] or 1
        out["support_roots"] = [
            r for r, w in session["weights"].most_common(3)
            if r != root and w / total >= 0.25
        ][:2]
        allowed = [root, *out["support_roots"]]
        scores = collections.Counter()
        last = {}
        for c in session["calls"]:
            if any(c["path"] == a or c["path"].startswith(a + "/") for a in allowed):
                scores[c["path"]] += c["weight"]
                last[c["path"]] = c["id"]
        out["key_paths"] = sorted(scores, key=lambda p: (scores[p], last[p]), reverse=True)[:8]
        visible = session["visible"]
        if visible and visible[-1]["role"] == "user":
            out["unresolved_user_feedback"] = visible[-1]["content"]
    return out


def _best_session(corpus: Corpus, root: str, relevance: dict[str, float]) -> dict | None:
    inside = [s for s in corpus.sessions.values() if s["weights"].get(root)]
    if not inside:
        return None
    return max(inside, key=lambda s: (relevance.get(s["id"], 0), s["weights"][root], s["last_ts"] or 0))


def _project_named(corpus: Corpus, name: str) -> str | None:
    """The single known project whose directory is called exactly `name`, or
    a directory of that name directly inside one (atlas/sim)."""
    projects = corpus.projects()
    exact = [p for p in projects if Path(p).name == name]
    if not exact:
        exact = [str(Path(p) / name) for p in projects if (Path(p) / name).is_dir()]
    return exact[0] if len(exact) == 1 else None


def _effective_session(corpus: Corpus, sid: str | None) -> str | None:
    """The session whose work is "here": a session continued after context
    compression starts empty, and its work lives in the one it continues."""
    seen = set()
    while sid and sid not in seen and not (corpus.sessions.get(sid) or {}).get("calls"):
        seen.add(sid)
        parent = corpus.parents.get(sid)
        if not parent:
            break
        sid = parent
    return sid


def _unworked_repositories(corpus: Corpus, projects: set[str]) -> set[str]:
    """Repositories next to known projects (or in home) with no recorded work."""
    home = corpus.resolver.home
    out = set()
    for parent in {home} | {Path(p).parent for p in projects}:
        try:
            children = list(parent.iterdir())
        except OSError:
            continue
        for d in children:
            try:
                if str(d) not in projects and not corpus.resolver._ignored(d) and (d / ".git").is_dir():
                    out.add(str(d))
            except OSError:
                continue
    return out


def _current_project(corpus: Corpus, sid: str | None) -> tuple[str | None, list[str]]:
    """(project, contenders) of the current session's recent work.

    The project is None when the session has no work or its recent work is
    split between projects with no clear leader; contenders lists them.
    """
    s = corpus.sessions.get(sid or "")
    if not s or not s["calls"]:
        return None, []
    recent_ids = set(sorted({c["id"] for c in s["calls"]})[-CURRENT_WINDOW:])
    weights = collections.Counter()
    for c in s["calls"]:
        if c["id"] in recent_ids:
            weights[c["root"]] += c["weight"]
    ranked = weights.most_common()
    if len(ranked) > 1 and ranked[0][1] < CLEAR_MARGIN * ranked[1][1]:
        return None, [r for r, w in ranked if w * CLEAR_MARGIN > ranked[0][1]][:3]
    return ranked[0][0], []


def recall(
    request: str,
    db_path: Path = DB,
    home: Path = HOME,
    hermes_home: Path | None = None,
    current_session_id: str | None = None,
    as_of: float | None = None,
    check_exists: bool = True,
    pins: list | None = None,
) -> dict:
    as_of = as_of or time.time() + 1
    home = Path(home)
    resolver = RootResolver(home, hermes_home or home / ".hermes")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        corpus = Corpus(conn, resolver, as_of)
        decision = _decide(request, corpus, current_session_id, pins or [])
    finally:
        conn.close()
    if decision["found"] and check_exists and not Path(decision["action_root"]).is_dir():
        missing = decision["action_root"]
        decision = _result(False, "abstain", candidates=[missing], reason=f"root no longer exists: {missing}")
    return decision


def _decide(request: str, corpus: Corpus, current_sid: str | None, pins: list) -> dict:
    intent = intent_text(request)
    current_sid = _effective_session(corpus, current_sid)

    # 1. pin: whole words of the intent, so "dem" never fires on "demás" and
    #    a pasted log that mentions Lisboa is not a request about Lisboa.
    low = intent.casefold()
    for pin in pins:
        terms = [str(t).casefold() for t in pin.get("match") or []]
        hits = sum(1 for t in terms if re.search(rf"(?<!\w){re.escape(t)}(?!\w)", low))
        if terms and hits >= min(pin.get("require_terms", 2), len(terms)):
            root = str(pin["action_root"]).rstrip("/")
            session = _best_session(corpus, root, {})
            res = _result(True, "pin", root, session, corpus, reason=pin.get("note", "pinned"))
            if session is None:
                res["key_paths"] = list(pin.get("key_paths") or [])
            res["support_roots"] = list(pin.get("support_roots") or res["support_roots"])
            return res

    # 2. explicit paths written by the user
    anchors = collections.Counter(
        r for r in (corpus.resolver.root(p) for p in paths_in_text(intent)) if r
    )
    if anchors:
        root, n = anchors.most_common(1)[0]
        if n / sum(anchors.values()) >= ANCHOR_SHARE:
            return _result(True, "anchor", root, _best_session(corpus, root, {}), corpus,
                           reason="path written in the request")

    # 2b. the status line of a pasted Claude Code session names its directory
    names = STATUSLINE.findall(request or "")
    if names:
        root = _project_named(corpus, names[-1])
        if root:
            return _result(True, "statusline", root, _best_session(corpus, root, {}), corpus,
                           reason="directory shown in a pasted Claude Code status line")

    # term statistics
    n_sessions = max(len(corpus.sessions), 1)
    excluded = negated_terms(intent)
    terms = [t for t in extract_terms(intent) if t.casefold() not in excluded]
    term_sessions = {t: corpus.document_frequency(t) for t in terms}
    # Smoothed so a term never weighs zero; generic terms are already filtered by df.
    idf = {t: math.log(1 + n_sessions / max(len(ss), 1)) for t, ss in term_sessions.items()}
    df_limit = max(DF_FLOOR, DISCRIMINATIVE_DF * n_sessions)
    disc = [t for t in terms if 0 < len(term_sessions[t]) <= df_limit
            or (not term_sessions[t] and any(name_match(t, p) for p in corpus.projects()))]

    relevance = collections.Counter()
    for t in disc:
        for sid in term_sessions[t]:
            relevance[sid] += idf[t]
    for s in corpus.sessions.values():
        for t in disc:
            if t.casefold() in s["title"].casefold() and s["id"] not in term_sessions[t]:
                relevance[s["id"]] += idf[t]

    # 3. a discriminative term names a project
    projects = corpus.projects()
    unworked = _unworked_repositories(corpus, projects)
    named = collections.Counter()
    for p in projects | unworked:
        for t in disc:
            score = name_match(t, p)
            if score:
                named[p] += score * max(idf[t], 1.0)
    if named:
        ranked = named.most_common()
        top, top_score = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0
        if top_score >= CLEAR_MARGIN * second and top in unworked:
            # "notenest" names ~/NoteNest; past turns that merely mentioned it
            # worked elsewhere, and there is no work of its own to resume.
            return _result(False, "abstain", candidates=[top], reason="named project has no recorded work")
        if top_score >= CLEAR_MARGIN * second:
            return _result(True, "name", top, _best_session(corpus, top, relevance), corpus,
                           reason="project named in the request")
        tied = [p for p, sc in ranked if sc * CLEAR_MARGIN > top_score]
        return _result(False, "abstain", candidates=tied[:3], reason="several projects match the name")

    # Specific terms: identifiers and proper names by form, or words so rare
    # that they point somewhere. Common words appear everywhere and decide
    # nothing, however low the document frequency of the whole phrase.
    named = named_terms(intent)
    # Rarity is evidence only once DF_FLOOR sessions are a small share of the
    # corpus; in a small one, "value" sits in a single session by chance.
    rare_limit = DF_FLOOR if RARE_SHARE * n_sessions >= DF_FLOOR else 0
    specific = [t for t in disc if t.casefold() in named or len(term_sessions[t]) <= rare_limit]

    current, contenders = _current_project(corpus, current_sid)
    # Leaving an ongoing session is a stronger move than choosing in a fresh
    # one, so only a term shaped like a name can trigger it.
    topic_shift = any(
        t.casefold() in named and current_sid not in term_sessions[t] for t in specific
    )

    # 4. an ongoing session is left only by naming another project (step 3),
    #    writing its path (step 2), or raising a specific topic it never
    #    discussed. Vague words ("tomorrow", "prompt") never move it.
    if not topic_shift:
        if current:
            return _result(True, "current", current, corpus.sessions[current_sid], corpus,
                           reason="continuing the work of the current session")
        if contenders:
            return _result(False, "abstain", candidates=contenders,
                           reason="current session's recent work is split between projects")
        if not specific:
            return _result(False, "abstain", reason="no specific topic and no work in this session")

    # 6. topic discussed in past sessions: each term votes for the projects
    #    worked on in the very turns that discussed it.
    content = collections.Counter()
    for t in specific:
        work = corpus.attributed_work(t)
        total = sum(work.values())
        for p, w in work.items():
            content[p] += idf[t] * w / total
    ranked = [(p, sc) for p, sc in content.most_common() if sc > 0]
    if not ranked:
        return _result(False, "abstain", reason="no past work matches the topic")

    top, top_score = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0
    if top_score >= CLEAR_MARGIN * second:
        return _result(True, "content", top, _best_session(corpus, top, relevance), corpus,
                       reason="past sessions about this topic worked here")
    tied = [p for p, sc in ranked if sc * CLEAR_MARGIN > top_score]
    return _result(False, "abstain", candidates=tied[:3], reason="topic spread across projects")


# --- output --------------------------------------------------------------------


def compact(text, limit=700):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def format_handoff(result: dict) -> str:
    lines = ["JARVIS_HANDOFF_V2"]
    if not result["found"]:
        lines += ["FOUND=0", f"REASON={result['reason']}"]
        lines += [f"CANDIDATE_{i}={c}" for i, c in enumerate(result["candidates"], 1)]
        lines += [
            "NEXT=ASK_USER_WHICH_PROJECT",
            "NEXT_OBJECTIVE=Recall could not identify the project with confidence. "
            "Ask the user which project to continue (offer the candidates if any). "
            "Do not guess and do not search for it yourself.",
        ]
        return "\n".join(lines)

    root = result["action_root"]
    lines += [
        "FOUND=1",
        f"MODE={result['mode']}",
        f"REASON={result['reason']}",
        f"SESSION_ID={result['session_id'] or ''}",
        f"TITLE={result['title'] or ''}",
        f"DOMINANT_ROOT={root}",
        f"ACTION_ROOT={root}",
    ]
    if result["support_roots"]:
        lines.append("SUPPORT_ROOTS=" + ";".join(result["support_roots"]))
    lines += [f"KEY_PATH_{i}={p}" for i, p in enumerate(result["key_paths"], 1)]
    if result["unresolved_user_feedback"]:
        lines.append("UNRESOLVED_USER_FEEDBACK=" + compact(result["unresolved_user_feedback"]))
        lines.append(
            "NEXT_OBJECTIVE=Reproduce the unresolved user-visible failure using ACTION_ROOT and "
            "KEY_PATHS, fix the actual cause, and verify the resulting behavior instead of "
            "trusting previous tests alone."
        )
    else:
        lines.append(
            "NEXT_OBJECTIVE=Inspect current live state only inside ACTION_ROOT/SUPPORT_ROOTS, "
            "continue the last genuine work, and verify the outcome."
        )
    lines += [
        "NEXT=LIVE_STATE_THEN_ACT_THEN_VERIFY",
        "SCOPE_LOCK=ACTION_ROOT,SUPPORT_ROOTS,KEY_PATHS",
        "FORBIDDEN_UNTIL_FIRST_ACTION=tool_search,tool_describe,tool_call,session_search,"
        "skills_search,broad_home_scan,broad_project_find,unrelated_project_subtree",
    ]
    return "\n".join(lines)


def load_pins(path: Path = PINS) -> list:
    if not path.exists():
        return []
    data = loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("pins")
    return [p for p in data if isinstance(p, dict) and p.get("action_root")] if isinstance(data, list) else []


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("request")
    ap.add_argument("--handoff", action="store_true", help="print the JARVIS_HANDOFF_V2 block (default)")
    ap.add_argument("--json", action="store_true", help="print the full decision as JSON")
    ap.add_argument("--db", type=Path, default=DB)
    ap.add_argument("--session", default=os.environ.get("HERMES_SESSION_ID"))
    ap.add_argument("--as-of", type=float, default=None, help="ignore messages at or after this epoch")
    args = ap.parse_args()

    if not args.db.exists():
        raise SystemExit(f"Missing DB: {args.db}")

    result = recall(
        args.request,
        db_path=args.db,
        current_session_id=args.session,
        as_of=args.as_of,
        check_exists=args.as_of is None,
        pins=load_pins(),
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(format_handoff(result))


if __name__ == "__main__":
    main()
