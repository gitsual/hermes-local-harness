"""Characterization tests for the jarvis-scope-guard Hermes plugin.

They exercise the hook functions directly, the same way Hermes calls them,
without needing a running Hermes or a model.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "jarvis-scope-guard" / "__init__.py"


@pytest.fixture
def guard(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    spec = importlib.util.spec_from_file_location("scope_guard_under_test", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def handoff(action_root: Path, *key_paths: Path) -> str:
    lines = [
        "JARVIS_HANDOFF_V2",
        "FOUND=1",
        f"DOMINANT_ROOT={action_root}",
        f"ACTION_ROOT={action_root}",
        "SUPPORT_ROOTS=",
        *(f"KEY_PATH_{i}={p}" for i, p in enumerate(key_paths, 1)),
        "SCOPE_LOCK=ACTION_ROOT,SUPPORT_ROOTS,KEY_PATHS",
        "NEXT=LIVE_STATE_THEN_ACT_THEN_VERIFY",
    ]
    return "\n".join(lines)


RECALL_CMD = "~/.hermes/skills/jarvis/jarvis-recall/scripts/jarvis-episode-recall.py 'continúa' --handoff"


def lock_scope(guard, root: Path, sid: str = "s1") -> None:
    guard.pre_llm_call(session_id=sid, user_message="continúa con lo que hicimos ayer")
    guard.post_tool_call(
        tool_name="terminal",
        args={"command": RECALL_CMD},
        result=handoff(root),
        session_id=sid,
    )


@pytest.mark.parametrize(
    "message",
    ["continúa", "retoma el build", "donde lo dejamos", "resume the previous task", "where we left off"],
)
def test_continuation_requests_are_detected(guard, message):
    assert guard._is_continuation(message)


@pytest.mark.parametrize("message", ["", "instala ripgrep", "write a hello world in Go"])
def test_fresh_requests_are_not_continuations(guard, message):
    assert not guard._is_continuation(message)


@pytest.mark.parametrize(
    "message",
    [
        "continue with the stripe webhook",
        "resume trailapp",
        "please carry on with the migration",
        "keep going with the parser",
        "pick up the billing work again",
        "redo what we did yesterday",
        "continue",
        "here is the log:\nerror: timeout\ncontinue with the retry fix",
    ],
)
def test_english_continuations_match_spanish_coverage(guard, message):
    assert guard._is_continuation(message)


@pytest.mark.parametrize(
    "message",
    [
        "update my resume.pdf with the new job",
        "the loop should continue after an error",
        "write a function that resumes interrupted downloads",
        "resume.pdf is out of date",
        "fix this build error:\nContinue? [y/N] n\nerror: aborted by user",
        "this dialog keeps showing up:\nSpace name\ne.g. Product Development\nContinue",
    ],
)
def test_english_words_inside_a_fresh_request_do_not_gate(guard, message):
    assert not guard._is_continuation(message)


def test_normal_turn_does_not_gate_tools(guard):
    assert guard.pre_llm_call(session_id="s1", user_message="instala ripgrep") is None
    assert guard.pre_tool_call(tool_name="terminal", args={"command": "ls"}, session_id="s1") is None


def test_continuation_injects_recall_instruction(guard):
    result = guard.pre_llm_call(session_id="s1", user_message="continúa")
    assert "jarvis-episode-recall.py" in result["context"]


def test_recall_phase_blocks_filesystem_inspection(guard):
    guard.pre_llm_call(session_id="s1", user_message="continúa")
    verdict = guard.pre_tool_call(tool_name="terminal", args={"command": "ls ~"}, session_id="s1")
    assert verdict["action"] == "block"
    assert "PHASE=RECALL_REQUIRED" in verdict["message"]


def test_recall_phase_allows_only_the_recall_skill_and_helper(guard):
    guard.pre_llm_call(session_id="s1", user_message="continúa")
    other_skill = guard.pre_tool_call(tool_name="skill_view", args={"name": "github"}, session_id="s1")
    assert other_skill["action"] == "block"
    assert guard.pre_tool_call(tool_name="skill_view", args={"name": "jarvis/jarvis-recall"}, session_id="s1") is None
    assert guard.pre_tool_call(tool_name="terminal", args={"command": RECALL_CMD}, session_id="s1") is None


def test_handoff_without_next_marker_keeps_recall_phase(guard, tmp_path):
    guard.pre_llm_call(session_id="s1", user_message="continúa")
    broken = handoff(tmp_path).replace("NEXT=LIVE_STATE_THEN_ACT_THEN_VERIFY", "")
    guard.post_tool_call(tool_name="terminal", args={"command": RECALL_CMD}, result=broken, session_id="s1")
    assert guard._state("s1")["phase"] == "RECALL_REQUIRED"


def test_handoff_locks_scope_to_action_root(guard, tmp_path):
    lock_scope(guard, tmp_path)
    state = guard._state("s1")
    assert state["phase"] == "SCOPE_LOCKED"
    assert state["action_root"] == str(tmp_path.resolve())


def test_scope_lock_allows_files_inside_and_blocks_outside(guard, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    lock_scope(guard, root)
    inside = guard.pre_tool_call(tool_name="read_file", args={"path": str(root / "main.py")}, session_id="s1")
    outside = guard.pre_tool_call(tool_name="read_file", args={"path": str(tmp_path / "other.py")}, session_id="s1")
    assert inside is None
    assert outside["action"] == "block"


def test_scope_lock_rejects_relative_paths(guard, tmp_path):
    lock_scope(guard, tmp_path)
    verdict = guard.pre_tool_call(tool_name="read_file", args={"path": "main.py"}, session_id="s1")
    assert verdict["action"] == "block"


def test_terminal_needs_an_in_scope_workdir(guard, tmp_path):
    lock_scope(guard, tmp_path)
    no_workdir = guard.pre_tool_call(tool_name="terminal", args={"command": "make"}, session_id="s1")
    assert no_workdir["action"] == "block"
    ok = guard.pre_tool_call(
        tool_name="terminal", args={"command": "make", "workdir": str(tmp_path)}, session_id="s1"
    )
    assert ok is None


def test_terminal_blocks_parent_directory_escapes(guard, tmp_path):
    lock_scope(guard, tmp_path)
    verdict = guard.pre_tool_call(
        tool_name="terminal", args={"command": "cat ../secrets", "workdir": str(tmp_path)}, session_id="s1"
    )
    assert verdict["action"] == "block"


def test_terminal_blocks_windows_parent_directory_escapes(guard, tmp_path):
    lock_scope(guard, tmp_path)
    verdict = guard.pre_tool_call(
        tool_name="terminal", args={"command": r"type ..\secrets", "workdir": str(tmp_path)}, session_id="s1"
    )
    assert verdict["action"] == "block"


def test_terminal_blocks_out_of_scope_home_paths(guard, tmp_path):
    root = Path.home() / ".hermes-scope-guard-test-root"
    lock_scope(guard, root)
    elsewhere = Path.home() / "some-other-project" / "file.txt"
    verdict = guard.pre_tool_call(
        tool_name="terminal", args={"command": f"cat {elsewhere}", "workdir": str(root)}, session_id="s1"
    )
    assert verdict["action"] == "block"


def test_scope_lock_disables_discovery_tools(guard, tmp_path):
    lock_scope(guard, tmp_path)
    for tool in ("session_search", "delegate_task", "skill_view", "memory"):
        assert guard.pre_tool_call(tool_name=tool, args={}, session_id="s1")["action"] == "block"


def test_next_user_turn_releases_the_lock(guard, tmp_path):
    lock_scope(guard, tmp_path)
    guard.pre_llm_call(session_id="s1", user_message="ahora instala jq")
    assert guard._state("s1") is None


def test_sessions_are_isolated(guard, tmp_path):
    lock_scope(guard, tmp_path, sid="locked")
    assert guard.pre_tool_call(tool_name="session_search", args={}, session_id="other") is None


def test_plugin_writes_its_log_under_hermes_home(guard, tmp_path):
    guard.pre_llm_call(session_id="s1", user_message="hola")
    assert Path(os.environ["HERMES_HOME"], "logs").is_dir()


def test_not_found_handoff_releases_the_gate(guard):
    guard.pre_llm_call(session_id="s1", user_message="continúa")
    guard.post_tool_call(
        tool_name="terminal",
        args={"command": RECALL_CMD},
        result="JARVIS_HANDOFF_V2\nFOUND=0\nNEXT=ASK_USER_WHICH_PROJECT",
        session_id="s1",
    )
    assert guard._state("s1") is None
    assert guard.pre_tool_call(tool_name="clarify", args={}, session_id="s1") is None


def test_pasted_material_with_a_late_continuation_word_is_not_a_continuation(guard):
    spec = "# PRD — Research system\n\n" + ("Requirement text. " * 80) + "El agente continúa la tarea."
    assert not guard._is_continuation(spec)


def test_system_notes_are_not_continuations(guard):
    note = "  [ASYNC DELEGATION BATCH COMPLETE — deleg_1] continúa con el resultado"
    assert not guard._is_continuation(note)


def test_continuation_inside_a_code_fence_is_ignored(guard):
    assert not guard._is_continuation("mira este log:\n```\nworker: continúa\n```")


def test_continuation_at_the_end_of_a_pasted_log_counts(guard):
    log = ("worker 3 finished batch\n" * 60) + "\ncontinúa"
    assert guard._is_continuation(log)
