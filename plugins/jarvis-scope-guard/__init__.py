"""Jarvis Recall-First + Scope Guard v8.2.

Two phases:

1) RECALL_REQUIRED
   A continuation/previous-work request is detected in pre_llm_call.
   Until the deterministic helper succeeds, the ONLY useful operations are:
     - skill_view(jarvis recall)
     - terminal(...jarvis-episode-recall.py ... --handoff)

   Everything else is mechanically blocked.

2) SCOPE_LOCKED
   post_tool_call parses JARVIS_HANDOFF_V2 FOUND=1 and captures ACTION_ROOT.
   From then on, discovery/recall tools are blocked and local filesystem/tool
   access is restricted to ACTION_ROOT/SUPPORT_ROOTS/KEY_PATHS.

This closes both observed failure modes:
- "I should run recall" -> ls/other repo instead
- correct recall -> later cross-project drift
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()

HERMES_HOME = Path(
    os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
).expanduser().resolve()

LOG_FILE = HERMES_HOME / "logs" / "jarvis-scope-v8.2.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

HOME = str(Path.home().resolve())

# session_id -> current user-turn state
_SESSIONS: dict[str, dict[str, Any]] = {}

# When a pre_tool payload omits session_id in a particular Hermes path,
# TUI normally has exactly one foreground turn. Keep a bounded fallback.
_LAST_ACTIVE_SESSION: str = ""

CONTINUATION_PATTERNS = [
    # Spanish
    r"\bcontin[uú]a\b",
    r"\bcontinuar\b",
    r"\blo que hicimos\b",
    r"\blo que hicimos (?:antes|ayer|la otra vez)\b",
    r"\brecupera\b.*\b(?:estado|contexto|trabajo|sesi[oó]n)\b",
    r"\b[uú]ltimo estado\b",
    r"\bdesde ah[ií]\b",
    r"\bretoma\b",
    r"\bretomar\b",
    r"\bdonde lo dejamos\b",
    r"\btrabajo anterior\b",
    # English
    r"\bcontinue\b.*\b(?:previous|prior|last|where we left)\b",
    r"\bresume\b.*\b(?:previous|prior|last)\b",
    r"\bwhere we left off\b",
    r"\brecover\b.*\b(?:state|context|session|work)\b",
]

CONTINUATION_RE = re.compile(
    "|".join(f"(?:{p})" for p in CONTINUATION_PATTERNS),
    re.IGNORECASE | re.DOTALL,
)

FIELD_RE = re.compile(r"(?m)^([A-Z][A-Z0-9_]+)=(.*)$")

HOME_PATH_RE = re.compile(
    re.escape(HOME) + r"/[^\s'\";|&><)\]}]+"
)

RECALL_SKILL_NAMES = {
    "jarvis-recall",
    "jarvis/jarvis-recall",
}

BLOCKED_AFTER_HANDOFF = {
    "session_search",
    "tool_search",
    "tool_describe",
    "delegate_task",
    "skill_view",
    "skill_manage",
    "skills_list",
    "setup_mcp",
    "execute_code",
    "memory",
}

FILE_TOOLS = {
    "read_file",
    "write_file",
    "patch",
    "search_files",
}

PATH_KEYS = {
    "path",
    "file",
    "file_path",
    "filepath",
    "filename",
    "directory",
    "dir",
    "root",
    "base_dir",
    "target_dir",
    "workdir",
    "cwd",
}


def _log(event: str, **fields: Any) -> None:
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "pid": os.getpid(),
        "event": event,
        **fields,
    }
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _sid(session_id: str = "", **kwargs: Any) -> str:
    global _LAST_ACTIVE_SESSION

    value = str(
        session_id
        or kwargs.get("session_id")
        or ""
    ).strip()

    if value:
        return value

    # Safe TUI fallback only when there is exactly one active guarded turn.
    with _LOCK:
        active = [
            sid
            for sid, state in _SESSIONS.items()
            if state.get("phase") in {"RECALL_REQUIRED", "SCOPE_LOCKED"}
        ]
        if len(active) == 1:
            return active[0]

    return _LAST_ACTIVE_SESSION if _LAST_ACTIVE_SESSION in _SESSIONS else ""


def _state(session_id: str) -> dict[str, Any] | None:
    if not session_id:
        return None
    with _LOCK:
        return _SESSIONS.get(session_id)


def _set_state(session_id: str, state: dict[str, Any]) -> None:
    global _LAST_ACTIVE_SESSION
    if not session_id:
        return
    with _LOCK:
        _SESSIONS[session_id] = state
        _LAST_ACTIVE_SESSION = session_id


def _clear(session_id: str) -> None:
    global _LAST_ACTIVE_SESSION
    if not session_id:
        return
    with _LOCK:
        _SESSIONS.pop(session_id, None)
        if _LAST_ACTIVE_SESSION == session_id:
            _LAST_ACTIVE_SESSION = ""


def _is_continuation(user_message: str) -> bool:
    text = str(user_message or "").strip()
    if not text:
        return False
    return CONTINUATION_RE.search(text) is not None


def _flatten_strings(value: Any):
    if isinstance(value, str):
        yield value
        try:
            parsed = json.loads(value)
        except Exception:
            return
        if parsed != value:
            yield from _flatten_strings(parsed)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _flatten_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _flatten_strings(item)


def _norm(path: str) -> str:
    return str(Path(os.path.expanduser(path)).resolve())


def _parse_handoff(result: Any) -> dict[str, Any] | None:
    text = "\n".join(_flatten_strings(result))

    if (
        "JARVIS_HANDOFF_V2" not in text
        or "FOUND=1" not in text
        or "NEXT=LIVE_STATE_THEN_ACT_THEN_VERIFY" not in text
    ):
        return None

    fields: dict[str, list[str]] = {}
    for name, value in FIELD_RE.findall(text):
        fields.setdefault(name, []).append(value.strip())

    action_root = (fields.get("ACTION_ROOT") or [""])[-1]
    dominant_root = (fields.get("DOMINANT_ROOT") or [""])[-1]
    action_root = action_root or dominant_root

    if not action_root:
        return None

    support_roots: list[str] = []
    for raw in fields.get("SUPPORT_ROOTS", []):
        support_roots.extend(
            item.strip()
            for item in raw.split(";")
            if item.strip()
        )

    key_paths: list[str] = []
    for name, values in fields.items():
        if name.startswith("KEY_PATH_"):
            key_paths.extend(v for v in values if v)

    action_root = _norm(action_root)
    support_roots = [_norm(p) for p in support_roots]
    key_paths = [_norm(p) for p in key_paths]

    allowed_roots: list[str] = []
    for root in [action_root, *support_roots]:
        if root not in allowed_roots:
            allowed_roots.append(root)

    return {
        "phase": "SCOPE_LOCKED",
        "action_root": action_root,
        "allowed_roots": allowed_roots,
        "key_paths": key_paths,
        "skill_loaded": True,
        "blocked_count": 0,
    }


def _inside(path: str, root: str) -> bool:
    try:
        p = Path(os.path.expanduser(path)).resolve()
        r = Path(root).resolve()
        return p == r or r in p.parents
    except Exception:
        return False


def _allowed(path: str, state: dict[str, Any]) -> bool:
    normalized = _norm(path)

    if normalized in set(state.get("key_paths") or []):
        return True

    return any(
        _inside(normalized, root)
        for root in state.get("allowed_roots") or []
    )


def _block(
    state: dict[str, Any],
    tool_name: str,
    reason: str,
    session_id: str,
) -> dict[str, str]:
    state["blocked_count"] = int(state.get("blocked_count", 0)) + 1

    phase = state.get("phase", "UNKNOWN")
    action_root = state.get("action_root", "")

    message = (
        "JARVIS MECHANICAL GATE BLOCKED THIS TOOL CALL. "
        f"PHASE={phase}. "
        + (f"ACTION_ROOT={action_root}. " if action_root else "")
        + reason
    )

    if phase == "RECALL_REQUIRED":
        message += (
            " This user request is a continuation of prior work. "
            "Do NOT inspect the filesystem, search sessions manually, list projects, "
            "or infer the project. The only permitted next steps are: "
            "(1) skill_view for jarvis/jarvis-recall if not already loaded; "
            "(2) terminal executing the deterministic "
            "jarvis-episode-recall.py with the FULL CURRENT USER REQUEST and "
            "--handoff. Continue with that exact recall protocol now."
        )
    else:
        message += (
            " Deterministic recall is complete. Preserve the CURRENT USER GOAL. "
            "Stay inside ACTION_ROOT/SUPPORT_ROOTS/KEY_PATHS and continue "
            "ACT -> VERIFY. Do not start another recall or switch project."
        )

    _log(
        "BLOCK",
        phase=phase,
        tool=tool_name,
        reason=reason,
        session_id=session_id,
        action_root=action_root,
        blocked_count=state["blocked_count"],
    )

    return {"action": "block", "message": message}


def _walk_paths(value: Any):
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str) and str(key).lower() in PATH_KEYS:
                yield item
            else:
                yield from _walk_paths(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_paths(item)


def _check_file_tool(
    tool_name: str,
    args: dict[str, Any],
    state: dict[str, Any],
    session_id: str,
) -> dict[str, str] | None:
    paths = [
        p
        for p in _walk_paths(args)
        if isinstance(p, str) and p.strip()
    ]

    if not paths:
        return _block(
            state,
            tool_name,
            f"{tool_name} requires an explicit absolute in-scope path.",
            session_id,
        )

    for raw in paths:
        expanded = os.path.expanduser(raw)

        if not os.path.isabs(expanded):
            return _block(
                state,
                tool_name,
                f"Relative path {raw!r} is forbidden while scope lock is active.",
                session_id,
            )

        if not _allowed(expanded, state):
            return _block(
                state,
                tool_name,
                f"Path {expanded!r} is outside the recovered project scope.",
                session_id,
            )

    return None


def _check_terminal(
    args: dict[str, Any],
    state: dict[str, Any],
    session_id: str,
) -> dict[str, str] | None:
    command = str(args.get("command") or "")
    workdir = str(args.get("workdir") or args.get("cwd") or "")

    if not workdir:
        return _block(
            state,
            "terminal",
            "terminal requires an explicit absolute workdir inside "
            "ACTION_ROOT/SUPPORT_ROOTS.",
            session_id,
        )

    workdir = os.path.expanduser(workdir)

    if not os.path.isabs(workdir):
        return _block(
            state,
            "terminal",
            f"terminal workdir {workdir!r} must be absolute.",
            session_id,
        )

    if not _allowed(workdir, state):
        return _block(
            state,
            "terminal",
            f"terminal workdir {workdir!r} is outside the recovered scope.",
            session_id,
        )

    if "../" in command or re.search(
        r"(^|[;&|]\s*)cd\s+\.\.(?:/|\s|$)",
        command,
    ):
        return _block(
            state,
            "terminal",
            "Parent-directory escape (`..`) is forbidden.",
            session_id,
        )

    for raw in HOME_PATH_RE.findall(command):
        path = raw.rstrip(".,:")
        if not _allowed(path, state):
            return _block(
                state,
                "terminal",
                f"Command references out-of-scope home path {path!r}.",
                session_id,
            )

    return None


def _recall_skill_name(args: dict[str, Any]) -> str:
    return str(
        args.get("name")
        or args.get("skill")
        or args.get("skill_name")
        or ""
    ).strip()


def _is_recall_helper(args: dict[str, Any]) -> bool:
    command = str(args.get("command") or "")
    return (
        "jarvis-episode-recall.py" in command
        and "--handoff" in command
    )


def pre_llm_call(
    session_id: str = "",
    user_message: str = "",
    conversation_history: list | None = None,
    **kwargs: Any,
):
    sid = _sid(session_id, **kwargs)

    # Every new user turn gets a fresh state.
    _clear(sid)

    continuation = _is_continuation(user_message)

    if continuation:
        _set_state(
            sid,
            {
                "phase": "RECALL_REQUIRED",
                "skill_loaded": False,
                "blocked_count": 0,
                "user_message": str(user_message),
            },
        )
        _log(
            "RECALL_REQUIRED",
            session_id=sid,
            user_preview=str(user_message)[:300],
            kwargs_ids={
                "turn_id": kwargs.get("turn_id"),
                "task_id": kwargs.get("task_id"),
            },
        )

        # Context injection is advisory; pre_tool_call is the mechanical guard.
        return {
            "context": (
                "JARVIS MECHANICAL RECALL GATE IS ACTIVE. "
                "This is a continuation request. Before any project inspection "
                "or action, load jarvis/jarvis-recall and execute "
                "jarvis-episode-recall.py with the full current user request "
                "and --handoff. Any other tool call will be blocked."
            )
        }

    _log(
        "NORMAL_TURN",
        session_id=sid,
        user_preview=str(user_message)[:200],
    )
    return None


def pre_tool_call(
    tool_name: str = "",
    args: dict | None = None,
    task_id: str = "",
    **kwargs: Any,
):
    # session_id is not a declared parameter here, so it stays inside kwargs.
    # Passing it positionally as well raises TypeError: got multiple values.
    sid = _sid(**kwargs)
    state = _state(sid)

    _log(
        "PRE_TOOL",
        session_id=sid,
        phase=(state or {}).get("phase"),
        tool=tool_name,
        task_id=task_id,
        args=args or {},
        kwargs_ids={
            "turn_id": kwargs.get("turn_id"),
            "session_id": kwargs.get("session_id"),
        },
    )

    if state is None:
        return None

    args = args or {}
    phase = state.get("phase")

    if phase == "RECALL_REQUIRED":
        if tool_name == "skill_view":
            name = _recall_skill_name(args)

            if name not in RECALL_SKILL_NAMES:
                return _block(
                    state,
                    tool_name,
                    f"Only Jarvis Recall skill may be loaded now; got {name!r}.",
                    sid,
                )

            if state.get("skill_loaded"):
                return _block(
                    state,
                    tool_name,
                    "Jarvis Recall skill is already loaded; execute the helper now.",
                    sid,
                )

            return None

        if tool_name == "terminal":
            if _is_recall_helper(args):
                return None

            return _block(
                state,
                tool_name,
                "Filesystem/shell inspection is forbidden before deterministic recall.",
                sid,
            )

        return _block(
            state,
            tool_name,
            f"{tool_name} is forbidden until deterministic Jarvis recall completes.",
            sid,
        )

    if phase == "SCOPE_LOCKED":
        if tool_name in BLOCKED_AFTER_HANDOFF:
            return _block(
                state,
                tool_name,
                f"{tool_name} is disabled after JARVIS_HANDOFF_V2 FOUND=1.",
                sid,
            )

        if tool_name == "terminal":
            return _check_terminal(args, state, sid)

        if tool_name in FILE_TOOLS:
            return _check_file_tool(tool_name, args, state, sid)

    return None


def post_tool_call(
    tool_name: str = "",
    args: dict | None = None,
    result: Any = "",
    task_id: str = "",
    duration_ms: int = 0,
    **kwargs: Any,
):
    # Same as pre_tool_call: session_id lives in kwargs, not as a named param.
    sid = _sid(**kwargs)
    state = _state(sid)

    _log(
        "POST_TOOL",
        session_id=sid,
        phase=(state or {}).get("phase"),
        tool=tool_name,
        task_id=task_id,
        duration_ms=duration_ms,
        result_preview=str(result)[:500],
    )

    if state is None:
        return None

    if state.get("phase") == "RECALL_REQUIRED":
        if tool_name == "skill_view":
            name = _recall_skill_name(args or {})
            if name in RECALL_SKILL_NAMES:
                state["skill_loaded"] = True
                _log(
                    "RECALL_SKILL_LOADED",
                    session_id=sid,
                    skill=name,
                )
            return None

        if tool_name == "terminal" and _is_recall_helper(args or {}):
            locked = _parse_handoff(result)

            if locked is None:
                _log(
                    "HANDOFF_FAILED",
                    session_id=sid,
                    result_preview=str(result)[:1500],
                )
                # Remain in RECALL_REQUIRED. The next non-helper call will
                # still be blocked.
                return None

            # Preserve original continuation text for audit.
            locked["user_message"] = state.get("user_message", "")
            _set_state(sid, locked)

            _log(
                "HANDOFF_LOCKED",
                session_id=sid,
                action_root=locked["action_root"],
                allowed_roots=locked["allowed_roots"],
                key_paths=locked["key_paths"][:15],
            )
            return None

    return None


def on_session_reset(session_id: str = "", **kwargs: Any):
    sid = _sid(session_id, **kwargs)
    _clear(sid)
    _log("SESSION_RESET", session_id=sid)


def on_session_finalize(session_id: str | None = None, **kwargs: Any):
    sid = _sid(session_id or "", **kwargs)
    _clear(sid)
    _log("SESSION_FINALIZE", session_id=sid)


def register(ctx):
    _log("REGISTER", plugin="jarvis-scope-guard", version="8.2.0")

    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("on_session_reset", on_session_reset)
    ctx.register_hook("on_session_finalize", on_session_finalize)

    _log(
        "REGISTERED_HOOKS",
        hooks=[
            "pre_llm_call",
            "pre_tool_call",
            "post_tool_call",
            "on_session_reset",
            "on_session_finalize",
        ],
    )
