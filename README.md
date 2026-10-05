# hermes-local-harness

Make [Hermes Agent](https://github.com/NousResearch/hermes-agent) usable with a
**local** model on a single 16 GB GPU, so it stops inventing tasks, stops
drifting into other projects, and stops narrating results that never happened.

Out of the box, a local model in Hermes fails in ways that look like "the model
is dumb" but are almost always configuration. This repo is the set of fixes
that turned `qwen3-coder` (30B-A3B) and `gpt-oss:20b` into working agents,
each one isolated with an A/B test before it was kept.

## Quick path

```bash
git clone <this repo> && cd hermes-local-harness
./install.sh                  # plugin + recall skill into ~/.hermes, with backups
# then follow the 5 manual steps it prints (config merge, Ollama drop-in, restart)
```

Verify:

```bash
ollama ps            # CONTEXT column must say 65536, PROCESSOR ideally 100% GPU
hermes prompt-size   # overhead should be well under 50% of the window
```

## What is in here

| Layer | File | Problem it fixes |
|-------|------|------------------|
| Hermes config | `config/hermes.fragment.yaml` | Tool calls emitted as text, empty replies, compression aborts, broken web search, context eaten by tool schemas |
| Ollama | `config/ollama/20-local-agent.conf` | 64k context spilling to CPU on 16 GB |
| Plugin | `plugins/jarvis-scope-guard/` | Model "continuing" work by guessing the project, then wandering into sibling repos |
| Skill | `skills/jarvis/jarvis-recall/` | Deterministic recall: finds the previous episode and emits a machine-readable handoff |
| Persona | `soul/SOUL.example.md` | Definition of done: "a command ran" is not "the task is finished" |
| Patches | `patches/` | Hermes source fixes that config cannot reach |

Every config value has a comment with the reason. The measurements behind them
are in [`docs/findings.md`](docs/findings.md).

## How the anti-invention gate works

The plugin hooks into Hermes and turns "continue what we did" into a two-phase
state machine, enforced mechanically on every tool call:

```
user: "continúa con lo de ayer"
        │
        ▼
RECALL_REQUIRED ── any tool except the recall skill/helper ──► BLOCKED
        │  helper prints JARVIS_HANDOFF_V2 FOUND=1 ACTION_ROOT=...
        ▼
SCOPE_LOCKED ──── path outside ACTION_ROOT, `..`, no workdir,
        │         session_search, delegation, more skills ──► BLOCKED
        ▼
next user message ──► lock released
```

Prompts only *ask* the model to behave; the plugin *refuses* the tool call and
tells the model why, so it retries in scope. In real use over ~430 turns the
gate fired 33 times, locked scope 28 times and blocked 38 drifting calls.

Continuation phrases are detected in Spanish and English
(`CONTINUATION_PATTERNS` in the plugin).

## Known limitations

- **Recall abstains rather than guesses.** It locks a project only from a pin,
  a path or Claude Code status line in the request, a project name, the
  current session's work (followed across context compression), or rare and
  name-shaped words whose past turns worked in one clear place. Anything else
  asks the user. Against every continuation request in a real `state.db`
  (51 labeled cases, `tools/eval-recall.py`) it makes no wrong lock, but all
  51 were also used to tune it: there is no untouched test set left. For a
  topic it gets wrong, add a pin (`examples/jarvis-episode-pins.example.json` →
  `~/.hermes/jarvis-episode-pins.json`).
- **A path with spaces typed without quotes is cut at the first space** and
  resolves to a directory that does not exist; live recall then abstains.
- **qwen3-coder at 64k does not fit fully in 16 GB.** Expect part of it on CPU
  (`ollama ps`). `gpt-oss:20b` fits at 64k with the q8_0 KV cache at 100% GPU.
- **Patches are tied to Hermes 0.21.0.** Re-check them after every update.
- **Installer and Ollama drop-in are Linux only** (bash + systemd). The plugin
  and the recall helper are plain Python and the whole test suite passes on
  Linux and Windows 11. On Windows, recall reads paths from tool arguments but
  only finds POSIX-style paths in free text.

## Tests

```bash
python3 -m pytest -q tests
```

The tests drive the plugin hooks directly, exactly as Hermes calls them. No
model or running Hermes is needed.

To score recall end to end against your own history, label past requests
(format in the docstring of `tools/eval-recall.py`) and run:

```bash
python3 tools/eval-recall.py ~/.hermes/jarvis-recall-eval.json -v
```

## License

MIT
