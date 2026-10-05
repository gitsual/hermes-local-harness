# Findings

Each fix below started as a symptom that looked like a model problem. In every
case the cause was the environment. Measured on Hermes Agent 0.21.0, Ollama,
RTX 4080 Super 16 GB.

## 1. qwen3-coder writes tool calls as text

**Symptom:** the reply contains `<function=skill_view><parameter=name>...` as
plain text, `finish_reason: stop`, `tool_calls: null`. Nothing runs.

**Cause:** Hermes injects `TOOL_USE_ENFORCEMENT_GUIDANCE` into models whose name
contains `qwen` (substring match in `agent/prompt_builder.py`). With that block,
qwen3-coder switches to a text tool-call format Hermes does not parse.

**Evidence:** A/B against Ollama, temperature 0, 22 tools, 34k-char system
prompt. Without the block: correct `tool_calls`. With it: broken, every run.
Prompt size and tool count were ruled out (22 tools + short prompt, 1 tool +
long prompt: both fine without the block).

**Fix:** list `agent.tool_use_enforcement` explicitly, without `qwen`.

**Do not generalize:** the same block does *not* break `gpt-oss` (6/6 correct
with and without). Measure per model.

## 2. The opposite: the agent stops after a plan

**Symptom:** the turn ends with "Plan of action" and nothing is executed.

**Cause:** a `!model` exclusion in `tool_use_enforcement` removed the block that
says "never end your turn with a promise of future action". The `!` prefix is
real and wins over every include.

**Fix:** only exclude models where the A/B shows the block breaks them.

## 3. Empty replies

**Symptom:** `content: ""`.

**Cause:** thinking models spend the budget reasoning; `/v1/chat/completions`
drops the reasoning block. A Modelfile `TEMPLATE` cannot fix it (Ollama parses
it as Go templates, not Jinja), and `SYSTEM /no_think` is lost as soon as the
client sends its own system prompt.

**Fix:** `agent.reasoning_overrides: {model: none}`, both with and without
`:latest`.

## 4. Hallucinated success and repeated paragraphs

**Symptom:** "mvn clean install succeeded, the jar was generated" for a command
that failed in 0.8 s. Whole paragraphs repeated verbatim.

**Cause:** the real output fell out of the context window. `hermes prompt-size`
showed 73% of a 32k window used before the first message, mostly by tool
schemas, which do not count as "system prompt".

| | before | after |
|---|---|---|
| tool schemas | 51,001 B (20 tools) | 28,371 B (14 tools) |
| overhead | 73% of window | 37% of window |

**Fix:** disable unused toolsets (`hermes tools disable ...`) and raise context
to 64k.

## 5. 64k context on 16 GB

Hermes refuses to run below 64,000 tokens (`MINIMUM_CONTEXT_LENGTH`, not
configurable). At fp16 KV cache, 65,536 spilled to CPU (12%/88%).

With `OLLAMA_FLASH_ATTENTION=1` + `OLLAMA_KV_CACHE_TYPE=q8_0`: `gpt-oss:20b` at
65,536 → 12 GB, 100% GPU, 140.7 tok/s (no slowdown vs 49,152).

**Measure carefully:** Ollama unloads asynchronously. Wait until `nvidia-smi`
shows VRAM freed before loading the next test, or results come out inverted.

Context precedence, lowest layer wins: `OLLAMA_CONTEXT_LENGTH` (default) <
Modelfile `PARAMETER num_ctx` < the model's architectural maximum (silent clamp).
`ollama show` reports what the model supports, not what the server delivers;
`ollama ps` shows the truth.

## 6. Compression aborts mid-session

**Cause:** `auxiliary.compression` on `provider: auto` uses whatever is loaded in
VRAM. If that model has < 64k context, Hermes aborts.

**Fix:** pin provider, model and `context_length`.

## 7. HTTP 500 `index $prop.Type 0` with gpt-oss

**Cause:** the gpt-oss chat template assumes every tool property has `type`.
One property declared as `anyOf` (`terminal.notify`) kills the whole session.
`type: [a, b]` does not help: Hermes' schema sanitizer rewrites unions back
into `anyOf`.

**How it was found:** a 25-line logging proxy between Hermes and Ollama to
capture the real payload; the registry schema does not show the final form.

**Fix:** `patches/terminal-notify-anyof.patch`. `systemctl reload` only rereads
config; patched Python needs a `restart`.

## 8. Web search fails out of the box

Empty `web.backend` resolves to firecrawl, which needs a key. Use `ddgs`.
