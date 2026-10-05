#!/usr/bin/env python3
from pathlib import Path
import re
import shutil
import time

home = Path.home()
tool = home / ".hermes/hermes-agent/tools/session_search_tool.py"

if not tool.is_file():
    candidates = list((home / ".hermes").glob("**/tools/session_search_tool.py"))
    if not candidates:
        raise SystemExit("tools/session_search_tool.py not found")
    tool = candidates[0]

s = tool.read_text(encoding="utf-8")
stamp = time.strftime("%Y%m%d-%H%M%S")
backup = tool.with_name(tool.name + f".jarvis-backup-{stamp}")
shutil.copy2(tool, backup)

# 1) Restore upstream default if an earlier hard user-only patch exists.
old_user_only = '''    # Local Jarvis recall policy:
    # Historical continuation should first match what the USER actually asked,
    # not assistant-generated summaries, tool-call serialization or recall echoes.
    #
    # The model can explicitly request role_filter="user,assistant" when a
    # user-only search genuinely returns no useful result.
    role_list = role_filter if role_filter else ["user"]'''

if old_user_only in s:
    s = s.replace(
        old_user_only,
        '    role_list = role_filter if role_filter else ["user", "assistant"]',
        1,
    )

# Also repair the one-line variant if present.
s = s.replace(
    'role_list = role_filter if role_filter else ["user"]',
    'role_list = role_filter if role_filter else ["user", "assistant"]',
)

# 2) Blank/no-arg browse guard.
marker = "Jarvis guardrail: session_search browse/blank query is disabled"

if marker not in s:
    pat = re.compile(
        r'^(?P<i>[ \t]*)if not query or not isinstance\(query, str\) or not query\.strip\(\):\n'
        r'(?P=i)[ \t]+return _list_recent_sessions\((?P<args>[^\n]+)\)',
        re.M,
    )
    m = pat.search(s)
    if not m:
        raise SystemExit(
            "No encuentro la rama upstream browse; revisa session_search_tool.py antes de parchear."
        )

    i = m.group("i")
    repl = f'''{i}# Local Jarvis safety policy:
{i}# blank/no-argument browse is disabled for the model-facing tool.
{i}if not query or not isinstance(query, str) or not query.strip():
{i}    return tool_error(
{i}        "Jarvis guardrail: session_search browse/blank query is disabled. "
{i}        "Use a non-empty targeted query, READ a known session_id, or SCROLL "
{i}        "using a session_id + around_message_id pair returned by the SAME "
{i}        "discovery result. Do not browse unrelated recent sessions.",
{i}        success=False,
{i}    )'''
    s = s[:m.start()] + repl + s[m.end():]

# 3) Strengthen the model-visible description if the known upstream phrase exists.
needle = '"Search past sessions stored in the local session DB, or scroll inside one. "'
if needle in s and "LOCAL JARVIS POLICY" not in s:
    s = s.replace(
        needle,
        '"LOCAL JARVIS POLICY: blank browse is disabled. Use targeted discovery; '
        'for scroll, session_id and around_message_id MUST come from the same result. '
        'Search past sessions stored in the local session DB, or scroll inside one. "',
        1,
    )

tool.write_text(s, encoding="utf-8")
print(f"patched: {tool}")
print(f"backup:  {backup}")
