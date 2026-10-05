"""Hermetic tests for jarvis-episode-recall.py.

Each scenario rebuilds, on a synthetic state.db and fake repositories, a
failure observed in real use. No Hermes install or model is needed.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "skills" / "jarvis" / "jarvis-recall" / "scripts" / "jarvis-episode-recall.py"
PLUGIN = ROOT / "plugins" / "jarvis-scope-guard" / "__init__.py"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def recall_mod():
    yield load(HELPER, "episode_recall_under_test")
    sys.modules.pop("episode_recall_under_test", None)


class World:
    """A fake home with repositories and a Hermes-shaped state.db."""

    def __init__(self, tmp_path: Path):
        self.home = tmp_path / "home"
        self.hermes = self.home / ".hermes"
        self.hermes.mkdir(parents=True)
        self.db_path = self.hermes / "state.db"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.executescript(
            """
            CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT,
                                   started_at REAL, parent_session_id TEXT);
            CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
                                   session_id TEXT, role TEXT, content TEXT,
                                   tool_calls TEXT, tool_call_id TEXT,
                                   timestamp REAL, active INTEGER DEFAULT 1);
            CREATE VIRTUAL TABLE messages_fts USING fts5(content);
            """
        )
        self.clock = 1_000.0
        self.calls = 0

    def filler(self, n: int = 150) -> None:
        """Unrelated past sessions, so word rarity means what it means in a real corpus."""
        for i in range(n):
            self.session(f"filler{i}", "", f"tarea rutinaria número {i}", edits(self.repo(f"work/filler{i % 7}"), 1))

    def repo(self, rel: str, marker: str = ".git") -> Path:
        path = self.home / rel
        path.mkdir(parents=True, exist_ok=True)
        if marker == ".git":
            (path / ".git").mkdir(exist_ok=True)
        elif marker:
            (path / marker).write_text("{}")
        return path

    def _msg(self, sid, role, content="", tool_calls=None, tool_call_id=None):
        self.clock += 10
        cur = self.conn.execute(
            "INSERT INTO messages (session_id, role, content, tool_calls, tool_call_id, timestamp)"
            " VALUES (?,?,?,?,?,?)",
            (sid, role, content, tool_calls, tool_call_id, self.clock),
        )
        fts = " ".join(x for x in (content, tool_calls) if x)
        self.conn.execute("INSERT INTO messages_fts (rowid, content) VALUES (?,?)", (cur.lastrowid, fts))
        return self.clock

    def session(self, sid: str, title: str = "", user: str = "", work=(), ok: bool = True, parent=None):
        """work: iterable of (tool, args_dict)."""
        self.clock += 100
        self.conn.execute(
            "INSERT INTO sessions (id, source, title, started_at, parent_session_id) VALUES (?,?,?,?,?)",
            (sid, "cli", title, self.clock, parent),
        )
        return self.turn(sid, user, work, ok)

    def turn(self, sid: str, user: str = "", work=(), ok: bool = True):
        """Append one user turn and the work done in response to it."""
        if user:
            self._msg(sid, "user", user)
        for tool, args in work:
            self.calls += 1
            call_id = f"call_{self.calls}"
            calls = json.dumps([{"id": call_id, "function": {"name": tool, "arguments": json.dumps(args)}}])
            self._msg(sid, "assistant", "", calls)
            result = '{"success": true}' if ok else '{"success": false, "error": "boom"}'
            self._msg(sid, "tool", result, tool_call_id=call_id)
        self._msg(sid, "assistant", "done")
        self.conn.commit()
        return self.clock

    def run(self, mod, request, session_id=None, as_of=None, check_exists=True, pins=None):
        return mod.recall(
            request,
            db_path=self.db_path,
            home=self.home,
            current_session_id=session_id,
            as_of=as_of,
            check_exists=check_exists,
            pins=pins or [],
        )


def edits(root: Path, n: int = 5, name: str = "main.py"):
    return [("write_file", {"path": str(root / f"src/{i}-{name}")}) for i in range(n)] + [
        ("terminal", {"command": "pytest -q", "workdir": str(root)})
    ]


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


# --- project root resolution -------------------------------------------------


def test_root_of_a_file_is_its_repository_not_the_file(recall_mod, world):
    repo = world.repo("Atlassian-Agent")
    resolver = recall_mod.RootResolver(world.home, hermes_home=world.hermes)
    assert resolver.root(str(repo / "pom.xml")) == str(repo)


def test_root_is_the_repository_not_its_container(recall_mod, world):
    repo = world.repo("work/proyectos/base-hmi")
    world.repo("work/proyectos/design-bridge")
    resolver = recall_mod.RootResolver(world.home, hermes_home=world.hermes)
    assert resolver.root(str(repo / "src/registry.json")) == str(repo)


def test_deleted_repository_resolves_from_observed_markers(recall_mod, world):
    gone = world.home / "work" / "dotfiles-portfolio"
    resolver = recall_mod.RootResolver(world.home, hermes_home=world.hermes)
    resolver.observe(str(gone / "package.json"))
    assert resolver.root(str(gone / "dotfiles/zshrc")) == str(gone)


def test_dependency_and_agent_paths_are_not_project_evidence(recall_mod, world):
    repo = world.repo("app")
    resolver = recall_mod.RootResolver(world.home, hermes_home=world.hermes)
    assert resolver.root(str(repo / "node_modules/react/index.js")) == str(repo)
    assert resolver.root(str(world.hermes / "skills/x/SKILL.md")) is None
    assert resolver.root(str(world.home)) is None


def test_paths_with_spaces_are_extracted_whole(recall_mod):
    cmd = 'ls "/home/u/Mis Documentos/Obsidian Vault/notes" && cat /home/u/app/a.py'
    assert recall_mod.paths_in_text(cmd) == [
        "/home/u/Mis Documentos/Obsidian Vault/notes",
        "/home/u/app/a.py",
    ]


def test_quoted_prose_is_not_mistaken_for_a_path(recall_mod):
    text = 'mira "/home/u/repo/ tengo una reunion con marta sobre esto y hay que integrarlo"'
    assert recall_mod.paths_in_text(text) == ["/home/u/repo"]


def test_overlong_paths_do_not_crash_resolution(recall_mod, world):
    resolver = recall_mod.RootResolver(world.home, hermes_home=world.hermes)
    resolver.root(str(world.home / ("x" * 300) / "a.py"))  # must not raise


# --- ranking -----------------------------------------------------------------


def test_named_project_beats_heavier_unrelated_work(recall_mod, world):
    heavy = world.repo("work/proyectos/base-hmi")
    named = world.repo("Atlassian-Agent")
    world.session("s1", "Integrar pipeline", "arregla el build del pipeline", edits(heavy, 25))
    world.session("s2", "", "compila Atlassian-Agent con maven", edits(named, 3))
    result = world.run(recall_mod, "continúa con el build de Atlassian-Agent")
    assert result["found"] and result["action_root"] == str(named)


def test_generic_terms_alone_do_not_select_a_project(recall_mod, world):
    for i in range(4):
        world.session(f"s{i}", "", "lanza el build", edits(world.repo(f"p{i}"), 3))
    result = world.run(recall_mod, "continúa con el build")
    assert not result["found"]


def test_topic_terms_in_past_conversation_select_the_project(recall_mod, world):
    world.filler()
    fotos = world.repo("work/proyecto-fotos")
    other = world.repo("work/otro")
    world.session("s1", "", "repara el disco con chkdsk desde la VM", edits(fotos, 4))
    world.session("s2", "", "refactoriza el router", edits(other, 9))
    for i in range(3):
        world.session(f"x{i}", "", "nada que ver", edits(world.repo(f"work/r{i}"), 2))
    result = world.run(recall_mod, "Continúa con chkdsk del WD10 en la VM")
    assert result["action_root"] == str(fotos)


def test_ambiguous_topic_abstains(recall_mod, world):
    a, b = world.repo("work/a"), world.repo("work/b")
    world.session("s1", "", "migra el login a OAuth", edits(a, 4))
    world.session("s2", "", "migra el login a OAuth", edits(b, 4))
    for i in range(3):
        world.session(f"x{i}", "", "nada que ver", edits(world.repo(f"work/r{i}"), 2))
    result = world.run(recall_mod, "continúa con OAuth")
    assert not result["found"]
    assert set(result["candidates"][:2]) == {str(a), str(b)}


def test_bare_continuation_stays_in_the_current_session_project(recall_mod, world):
    here = world.repo("work/trailapp")
    world.session("other", "", "algo", edits(world.repo("work/atlas"), 30))
    world.session("cur", "", "arregla el mapa", edits(here, 3))
    result = world.run(recall_mod, "continúa", session_id="cur")
    assert result["action_root"] == str(here)


def test_bare_continuation_in_a_fresh_session_abstains(recall_mod, world):
    world.session("old", "", "algo", edits(world.repo("work/atlas"), 30))
    result = world.run(recall_mod, "continúa")
    assert not result["found"]


def test_explicit_path_in_request_anchors_the_project(recall_mod, world):
    target = world.repo("work/dotfiles-portfolio")
    world.session("s1", "", "otra cosa", edits(world.repo("work/cci"), 20))
    world.session("s2", "", "dotfiles", edits(target, 2))
    result = world.run(recall_mod, f"Retoma mi portfolio en `{target}` desde su estado actual")
    assert result["action_root"] == str(target)


def test_failed_tool_calls_are_not_evidence(recall_mod, world):
    world.filler()
    real, noise = world.repo("work/real"), world.repo("work/noise")
    world.session("s1", "", "toca el parser de facturas", edits(noise, 20), ok=False)
    world.session("s2", "", "toca el parser de facturas", edits(real, 3))
    for i in range(3):
        world.session(f"x{i}", "", "nada", edits(world.repo(f"work/r{i}"), 2))
    result = world.run(recall_mod, "continúa con el parser de facturas")
    assert result["action_root"] == str(real)


def test_future_sessions_are_invisible_with_as_of(recall_mod, world):
    world.filler()
    past = world.repo("work/past")
    cutoff = world.session("s1", "", "trabaja en el exportador", edits(past, 3))
    for i in range(3):
        world.session(f"x{i}", "", "nada", edits(world.repo(f"work/r{i}"), 2))
    world.session("s2", "", "trabaja en el exportador", edits(world.repo("work/future"), 30))
    result = world.run(recall_mod, "continúa con el exportador", as_of=cutoff + 1)
    assert result["action_root"] == str(past)


def test_action_root_is_the_whole_repository(recall_mod, world):
    repo = world.repo("work/billing-api")
    work = [("write_file", {"path": str(repo / "web" / f"{i}.js")}) for i in range(6)]
    world.session("s1", "", "arregla la consola web de billing-api", work)
    result = world.run(recall_mod, "continúa con billing-api")
    assert result["action_root"] == str(repo)


def test_missing_root_on_disk_abstains_in_live_mode(recall_mod, world):
    gone = world.home / "work" / "gone-app"
    work = [("write_file", {"path": str(gone / "package.json")})] + edits(gone, 3)
    world.session("s1", "", "trabaja en gone-app", work)
    assert not world.run(recall_mod, "continúa con gone-app")["found"]
    assert world.run(recall_mod, "continúa con gone-app", check_exists=False)["found"]


def test_pin_overrides_ranking(recall_mod, world):
    pinned = world.repo("work/atlas/sim", marker="package.json")
    world.repo("work/atlas")
    world.session("s1", "", "cadastre", edits(world.repo("work/cci"), 20))
    pins = [{"match": ["cadastre", "lisboa"], "action_root": str(pinned)}]
    result = world.run(recall_mod, "continúa con Cadastre y Lisboa", pins=pins)
    assert result["action_root"] == str(pinned)


def test_a_pin_term_must_be_a_whole_word_in_the_intent(recall_mod, world):
    pinned = world.repo("work/atlas/sim", marker="package.json")
    world.session("s1", "", "nada", edits(world.repo("work/libro"), 3))
    pins = [{"match": ["lisboa", "dem"], "action_root": str(pinned)}]
    # "dem" inside "demás" and "Lisboa" deep in a pasted log are not the user asking for it.
    pasted = "lo demás está listo\n" + "x " * 2000 + "\nruta: Lisboa\n" + "y " * 2000 + "\ncontinua"
    assert world.run(recall_mod, pasted, pins=pins)["mode"] != "pin"
    assert world.run(recall_mod, "lo demás de Lisboa", pins=pins)["mode"] != "pin"


def test_pasted_claude_code_statusline_names_the_project(recall_mod, world):
    world.filler()
    cci = world.repo("work/billing-api")
    world.session("s1", "", "arregla la consola", edits(cci, 10))
    world.session("s2", "", "nada", edits(world.repo("chroma_diag_venv"), 5))
    request = (
        "Mira la memoria y continua: \n\n● Bash(.venv/bin/python /tmp/x/check_tile.py web/base.css)\n"
        + "─" * 300
        + "\n  Opus 5 (1M context) │ billing-api ███░░░░░░░ 31%\n  ⏵⏵ bypass permissions on · 5 shells\n"
    )
    result = world.run(recall_mod, request)
    assert (result["mode"], result["action_root"]) == ("statusline", str(cci))


def test_pasted_paths_are_not_topic_words(recall_mod):
    terms = recall_mod.extract_terms("error en /usr/share/nvim/runtime/lua/vim/treesitter.lua y .venv/bin/python")
    assert terms == ["error"]


def test_a_typed_line_before_a_paste_is_the_intent(recall_mod):
    paste = "\n".join(f"  línea {i} del transcript con Operaciones y settings-list" for i in range(60))
    intent = recall_mod.intent_text("Continúa con lo pendiente\n" + paste)
    assert intent.startswith("Continúa con lo pendiente\n") and "línea 0 " not in intent


def test_a_transcript_line_at_the_end_is_not_typed_intent(recall_mod):
    paste = "# Informe\n" + "texto pegado " * 100
    intent = recall_mod.intent_text(paste + "\n  ⎿  Interrupted · What should Claude do instead?")
    assert "Interrupted" not in intent


def test_rarity_means_nothing_in_a_small_corpus(recall_mod, world):
    noise = world.repo("work/libro")
    world.session("s1", "", "el valor INTRO del capítulo", edits(noise, 20))
    for i in range(40):
        world.session(f"x{i}", "", f"tarea {i}", edits(world.repo(f"work/r{i % 5}"), 1))
    assert not world.run(recall_mod, "arregla el valor INTRO")["found"]


def test_a_session_continued_after_compression_keeps_its_parents_project(recall_mod, world):
    app = world.repo("work/TrailApp")
    world.session("x0", "", "nada", edits(world.repo("work/otro"), 9))
    world.session("parent", "", "corrige la revisión", edits(app, 6))
    world.session("child", "", "", (), parent="parent")
    result = world.run(recall_mod, "continua hasta el final", session_id="child")
    assert (result["mode"], result["action_root"]) == ("current", str(app))


def test_naming_a_repository_with_no_recorded_work_abstains(recall_mod, world):
    world.filler()
    world.repo("NoteNest")
    tri = world.repo("tri")
    world.session("s1", "", "el proyecto de engram se llama notenest", edits(tri, 8))
    result = world.run(recall_mod, "Mira la memoria de notenest y continua")
    assert not result["found"]


def test_an_unreadable_directory_does_not_break_recall(recall_mod, world, monkeypatch):
    world.repo("work/app")
    world.session("s1", "", "arregla la app", edits(world.repo("work/app"), 5))
    real = Path.is_dir

    def is_dir(self):
        if self.name == ".git" and self.parent.name == "locked":
            raise PermissionError(13, "Permission denied")
        return real(self)

    (world.home / "work" / "locked").mkdir()
    monkeypatch.setattr(Path, "is_dir", is_dir)
    assert world.run(recall_mod, "continúa con app")["found"]


def test_a_topic_votes_for_the_work_done_in_its_own_turn(recall_mod, world):
    world.filler()
    facturas, dni = world.repo("work/facturas"), world.repo("work/dni-reader")
    world.session("s1", "", "arregla el parser de facturas", edits(facturas, 20))
    world.turn("s1", "ahora configura el lector de dnis", edits(dni, 3))
    for i in range(3):
        world.session(f"x{i}", "", "nada", edits(world.repo(f"work/r{i}"), 2))
    result = world.run(recall_mod, "continúa con el lector de dnis")
    assert result["action_root"] == str(dni)


def test_a_vague_topic_does_not_pull_an_ongoing_session_elsewhere(recall_mod, world):
    here, other = world.repo("work/portfolio"), world.repo("work/descargador")
    world.session("s1", "", "organiza descargas y prompts", edits(other, 10))
    for i in range(3):
        world.session(f"x{i}", "", "nada", edits(world.repo(f"work/r{i}"), 2))
    world.session("cur", "", "crea el portfolio", edits(here, 3))
    result = world.run(recall_mod, "retomamos mañana, déjame un prompt en descargas", session_id="cur")
    assert result["action_root"] == str(here)


def test_a_negated_project_name_is_not_an_anchor(recall_mod, world):
    cci = world.repo("work/billing-api")
    world.session("s1", "", "arregla la consola", edits(cci, 10))
    result = world.run(recall_mod, "continúa con los dotfiles; no uses ni modifiques billing-api")
    assert result["action_root"] != str(cci)


def test_explicitly_named_project_can_leave_an_ongoing_session(recall_mod, world):
    here, named = world.repo("work/portfolio"), world.repo("work/trailapp")
    world.session("s1", "", "mapa", edits(named, 5))
    world.session("cur", "", "crea el portfolio", edits(here, 3))
    result = world.run(recall_mod, "continúa con trailapp", session_id="cur")
    assert result["action_root"] == str(named)


def test_a_named_topic_the_current_session_never_discussed_is_a_topic_shift(recall_mod, world):
    past, here = world.repo("work/dotfiles-portfolio"), world.repo("work/cci")
    world.session("s1", "", "inventaria la config de Arch para publicarla", edits(past, 4))
    for i in range(3):
        world.session(f"x{i}", "", "nada", edits(world.repo(f"work/r{i}"), 2))
    world.session("cur", "", "configura el lector de dni", edits(here, 6))
    result = world.run(recall_mod, "continúa con lo de Arch", session_id="cur")
    assert result["action_root"] == str(past)


def test_current_session_split_between_projects_abstains(recall_mod, world):
    a, b = world.repo("work/a"), world.repo("work/b")
    world.session("cur", "", "mira las dos cosas", edits(a, 2) + edits(b, 2))
    result = world.run(recall_mod, "continúa", session_id="cur")
    assert not result["found"]


def test_common_words_do_not_decide_a_project(recall_mod, world):
    lots, few = world.repo("work/lots"), world.repo("work/few")
    for i in range(3):
        world.session(f"l{i}", "", "revisa la memoria y la auditoría", edits(lots, 4))
    world.session("f0", "", "revisa la memoria y la auditoría", edits(few, 1))
    for i in range(4):
        world.session(f"x{i}", "", "nada", edits(world.repo(f"work/r{i}"), 2))
    result = world.run(recall_mod, "Nueva ejecución\nEjecuciones\nAuditoría\nMemoria\nMotor continua")
    assert not result["found"]


@pytest.mark.parametrize(
    "text, named",
    [
        ("continúa con el portfolio de Arch", {"arch"}),
        ("Continúa con chkdsk del WD10 en la VM", {"wd10", "vm"}),
        ("continúa ComfyUI MASKDBG_RAWSEG mask.py", {"comfyui", "maskdbg_rawseg", "mask.py"}),
        ("Nueva ejecución\nEjecuciones\nAuditoría", set()),
        ("continúa con el lector de dnis", set()),
        ("Pulse INTRO o escriba una orden para continuar", set()),
        (".rw-r--r--  35k alex 27 may cap1.txt", {"cap1.txt"}),
    ],
)
def test_specific_terms_by_form(recall_mod, text, named):
    assert recall_mod.named_terms(text) == named


@pytest.mark.parametrize("text", ["continuing at 6:20pm", "usa 512MB y 30s de timeout", "a las 10am"])
def test_times_and_units_are_not_identifiers(recall_mod, text):
    assert recall_mod.named_terms(text) == set()


def test_times_and_units_are_not_topic_terms(recall_mod):
    assert recall_mod.extract_terms("esc to cancel at 6:20pm, 512MB") == ["esc", "cancel"]


def test_pasted_code_is_not_intent(recall_mod):
    code = "api[nid] = {\n    'class_type': 'SaveImage',\n    'inputs': {'filename_prefix': prefix},\n}\n" * 30
    intent = recall_mod.intent_text(code + "esc to cancel continua")
    assert "class_type" not in intent
    assert "continua" in intent


# --- handoff contract with the plugin ----------------------------------------


def test_found_handoff_locks_the_plugin_to_the_project(recall_mod, world, tmp_path, monkeypatch):
    repo = world.repo("work/app")
    world.session("s1", "", "arregla app", edits(repo, 3))
    text = recall_mod.format_handoff(world.run(recall_mod, "continúa con app"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    plugin = load(PLUGIN, "scope_guard_contract")
    plugin.pre_llm_call(session_id="t", user_message="continúa con app")
    plugin.post_tool_call(
        tool_name="terminal",
        args={"command": "jarvis-episode-recall.py 'x' --handoff"},
        result=text,
        session_id="t",
    )
    assert plugin._state("t")["action_root"] == str(repo.resolve())


def test_not_found_handoff_asks_the_user(recall_mod, world):
    text = recall_mod.format_handoff(world.run(recall_mod, "continúa"))
    assert "FOUND=0" in text
    assert "NEXT=ASK_USER_WHICH_PROJECT" in text
