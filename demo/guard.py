"""Replay one continuation turn through the scope-guard plugin hooks.

Each tool call the model "tries" goes through pre_tool_call exactly as Hermes
sends it; the verdict printed is the plugin's own. The recall step runs the
real helper against the fictional home built by demo/world.py.

    python3 demo/guard.py /tmp/demo/home
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "skills/jarvis/jarvis-recall/scripts/jarvis-episode-recall.py"
DIM, RED, GREEN, CYAN, BOLD, RESET = "\033[2m", "\033[31m", "\033[32m", "\033[36m", "\033[1m", "\033[0m"


def load_plugin():
    spec = importlib.util.spec_from_file_location("guard", ROOT / "plugins/jarvis-scope-guard/__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def show(label: str, text: str, pause: float = 0.9) -> None:
    print(f"{label} {text}", flush=True)
    time.sleep(pause)


def attempt(guard, tool: str, args: dict, shown: str) -> None:
    verdict = guard.pre_tool_call(tool_name=tool, args=args, session_id="demo")
    show(f"{DIM}model ›{RESET}", f"{tool}  {shown}", 0.5)
    if verdict and verdict.get("action") == "block":
        # The message is addressed to the model; the first sentence after the
        # phase and root is the reason.
        sentences = [s.strip() for s in verdict["message"].split(". ")]
        reason = next(s for s in sentences if not s.startswith(("JARVIS", "PHASE=", "ACTION_ROOT=")))
        reason = reason.replace(str(Path.home()), "~").replace(os.environ["HOME"], "~")
        show(f"  {RED}{BOLD}BLOCKED{RESET}", f"{DIM}{reason[:90]}{RESET}", 1.3)
    else:
        show(f"  {GREEN}{BOLD}allowed{RESET}", "", 0.8)


def main() -> None:
    home = Path(sys.argv[1])
    os.environ["HERMES_HOME"] = str(home / ".hermes")
    os.environ["HOME"] = str(home)
    guard = load_plugin()
    billing = home / "work/billing-api"
    request = "continúa con el webhook de stripe"

    show(f"{CYAN}{BOLD}user  ›{RESET}", request, 1.2)
    guard.pre_llm_call(session_id="demo", user_message=request)
    show(f"{DIM}gate  ›{RESET}", f"{BOLD}RECALL_REQUIRED{RESET}", 1.0)

    attempt(guard, "terminal", {"command": "ls ~/work"}, "ls ~/work")
    attempt(guard, "session_search", {"query": "stripe"}, "query=stripe")

    cmd = f"{HELPER} '{request}' --handoff"
    attempt(guard, "terminal", {"command": cmd}, "jarvis-episode-recall.py '…' --handoff")
    handoff = subprocess.run([sys.executable, str(HELPER), request], capture_output=True, text=True).stdout
    for line in handoff.splitlines()[:5] + [l for l in handoff.splitlines() if l.startswith("ACTION_ROOT")]:
        show(f"  {DIM}│{RESET}", line.replace(str(home), "~"), 0.25)
    guard.post_tool_call(tool_name="terminal", args={"command": cmd}, result=handoff, session_id="demo")
    show(f"{DIM}gate  ›{RESET}", f"{BOLD}SCOPE_LOCKED{RESET}  ~/work/billing-api", 1.2)

    attempt(guard, "read_file", {"path": str(home / "work/trailapp/README.md")}, "~/work/trailapp/README.md")
    attempt(guard, "terminal", {"command": "cat ../trailapp/build.gradle", "workdir": str(billing)}, "cat ../trailapp/build.gradle")
    attempt(guard, "read_file", {"path": str(billing / "app/webhooks/retry.py")}, "~/work/billing-api/app/webhooks/retry.py")
    attempt(guard, "terminal", {"command": "pytest -q tests/test_retry.py", "workdir": str(billing)}, "pytest -q  (workdir=billing-api)")


if __name__ == "__main__":
    main()
