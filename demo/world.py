"""Build a fictional home with three projects and a Hermes-shaped state.db.

Used by the README recordings so every frame shows real helper and plugin
output without touching anyone's real history:

    python3 demo/world.py /tmp/hlh-demo      # prints the HOME it built
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT,
                       started_at REAL, parent_session_id TEXT);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
                       session_id TEXT, role TEXT, content TEXT,
                       tool_calls TEXT, tool_call_id TEXT,
                       timestamp REAL, active INTEGER DEFAULT 1);
CREATE VIRTUAL TABLE messages_fts USING fts5(content);
"""


class World:
    def __init__(self, base: Path):
        shutil.rmtree(base, ignore_errors=True)
        self.home = base / "home"
        hermes = self.home / ".hermes"
        hermes.mkdir(parents=True)
        self.conn = sqlite3.connect(hermes / "state.db")
        self.conn.executescript(SCHEMA)
        self.clock = 1_780_000_000.0
        self.calls = 0

    def repo(self, rel: str) -> Path:
        path = self.home / rel
        (path / ".git").mkdir(parents=True, exist_ok=True)
        return path

    def _msg(self, sid, role, content="", tool_calls=None, tool_call_id=None):
        self.clock += 30
        cur = self.conn.execute(
            "INSERT INTO messages (session_id, role, content, tool_calls, tool_call_id, timestamp)"
            " VALUES (?,?,?,?,?,?)",
            (sid, role, content, tool_calls, tool_call_id, self.clock),
        )
        fts = " ".join(x for x in (content, tool_calls) if x)
        self.conn.execute("INSERT INTO messages_fts (rowid, content) VALUES (?,?)", (cur.lastrowid, fts))

    def session(self, sid: str, title: str, user: str, work) -> None:
        self.clock += 3600
        self.conn.execute(
            "INSERT INTO sessions (id, source, title, started_at, parent_session_id) VALUES (?,?,?,?,?)",
            (sid, "cli", title, self.clock, None),
        )
        self._msg(sid, "user", user)
        for tool, args in work:
            self.calls += 1
            call_id = f"call_{self.calls}"
            calls = json.dumps([{"id": call_id, "function": {"name": tool, "arguments": json.dumps(args)}}])
            self._msg(sid, "assistant", "", calls)
            self._msg(sid, "tool", '{"success": true}', tool_call_id=call_id)
        self._msg(sid, "assistant", "done")
        self.conn.commit()


def edits(root: Path, files: list[str], test: str = "pytest -q"):
    return [("write_file", {"path": str(root / f)}) for f in files] + [
        ("terminal", {"command": test, "workdir": str(root)})
    ]


def build(base: Path) -> Path:
    w = World(base)
    billing = w.repo("work/billing-api")
    trail = w.repo("work/trailapp")
    notes = w.repo("notes/notenest")
    for i in range(160):  # routine history, so word rarity means something
        w.session(f"r{i}", "", f"routine chore number {i}", edits(w.repo(f"work/chores{i % 5}"), ["a.py"]))
    w.session("s1", "Stripe webhook retries", "add idempotent retries to the stripe webhook handler",
              edits(billing, ["app/webhooks/stripe.py", "app/webhooks/retry.py", "tests/test_retry.py"]))
    w.session("s2", "Offline tiles", "cache map tiles offline in trailapp",
              edits(trail, ["app/src/main/java/tiles/TileCache.kt", "app/src/main/java/tiles/Store.kt"],
                    "./gradlew test"))
    w.session("s3", "Backlinks panel", "show backlinks in the notenest sidebar",
              edits(notes, ["src/sidebar/backlinks.ts", "src/index/links.ts"], "npm test"))
    w.session("s4", "Webhook signature", "verify the stripe webhook signature before retrying",
              edits(billing, ["app/webhooks/signature.py", "tests/test_signature.py"]))
    return w.home


if __name__ == "__main__":
    print(build(Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/hlh-demo")))
