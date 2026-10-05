#!/usr/bin/env python3
"""Evaluate recall end to end against labeled past requests.

Each case: {"id", "request", "session_id", "as_of", "expect": [roots...]}.
An empty "expect" means the right answer is to NOT lock any project; a null
inside "expect" means abstaining is also acceptable; "@current" accepts any
project the session had already worked in before the request (staying put
is harmless when the message was not really a continuation).

Runs the plugin's continuation gate first, then the recall helper with the
case's as_of cutoff, so no future session can leak into the decision.
Reports wrong locks separately: a wrong lock is the expensive failure.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def matches(root: str, expected: str) -> bool:
    return root == expected or root.startswith(expected.rstrip("/") + "/")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cases", type=Path)
    ap.add_argument("--db", type=Path, default=Path.home() / ".hermes" / "state.db")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    plugin = load(REPO / "plugins" / "jarvis-scope-guard" / "__init__.py", "scope_guard_eval")
    helper = load(REPO / "skills" / "jarvis" / "jarvis-recall" / "scripts" / "jarvis-episode-recall.py", "recall_eval")
    cases = json.loads(args.cases.read_text(encoding="utf-8"))

    correct = wrong = abstain_bad = 0
    for case in cases:
        expect = list(case["expect"])
        if "@current" in expect:
            import sqlite3
            conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
            corpus = helper.Corpus(conn, helper.RootResolver(helper.HOME), case["as_of"])
            conn.close()
            session = corpus.sessions.get(case.get("session_id") or "")
            expect = [e for e in expect if e != "@current"] + list(session["weights"] if session else [])
        if plugin._is_continuation(case["request"]):
            result = helper.recall(
                case["request"],
                db_path=args.db,
                current_session_id=case.get("session_id"),
                as_of=case.get("as_of"),
                check_exists=False,
                pins=helper.load_pins(),
            )
            root = result["action_root"] if result["found"] else None
            mode = result["mode"]
        else:
            root, mode = None, "not-a-continuation"

        if root is None:
            ok = not expect or None in expect
            verdict = "OK " if ok else "MISS"
            abstain_bad += not ok
        else:
            ok = any(e and matches(root, e) for e in expect)
            verdict = "OK " if ok else "WRONG"
            wrong += not ok
        correct += ok
        if args.verbose or not ok:
            print(f"{verdict:5} {case['id']:<26} {mode:<18} -> {root}  | want {expect}")

    total = len(cases)
    print(f"\ncorrect {correct}/{total}   wrong locks {wrong}   missed {abstain_bad}")
    return 0 if wrong == 0 and abstain_bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
