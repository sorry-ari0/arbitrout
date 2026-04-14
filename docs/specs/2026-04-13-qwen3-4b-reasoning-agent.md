# Spec: Qwen3-4B reasoning agent for OpenClaw (2026-04-13)

Add a dedicated **`reasoning`** agent in OpenClaw, backed by `qwen3:4b`, for tasks that benefit from a chain-of-thought trace. Keep the existing `main` agent (`llama-agent:latest`, Llama-3.1 8B) as the orchestrator. Arbitrout is the primary consumer.

## Motivation

Warm tool-calling benchmark on the Arc 140V (single-GPU, `MAX_LOADED_MODELS=1`):

| Model        | Params | VRAM   | Speed       | Tokens / call | Total / call | `<think>` blocks |
| ------------ | ------ | ------ | ----------- | ------------- | ------------ | ---------------- |
| llama-agent  | 8 B    | ~5 GB  | 10.5 tok/s  | 18            | **1.7 s**    | none             |
| qwen3:4b     | 4 B    | ~3 GB  | 14.1 tok/s  | 104           | 7.4 s        | yes              |
| qwen3:4b /no_think | 4 B | ~3 GB | 15.1 tok/s | 125           | 8.3 s        | yes (ignored)    |
| qwen3:8b     | 8 B    | ~5 GB  | 9.4 tok/s   | 124           | 13.2 s       | yes              |

Conclusion:

- `llama-agent` is **~4× faster end-to-end per tool call** because it does not emit thinking blocks. It stays as `main`.
- `qwen3:4b` is faster **per token** and produces a structured reasoning trace, which is exactly what arbitrout needs for analytical tasks (strategy classification, journal post-mortems, calibration audits, synthetic strategy generation).
- `qwen3:8b` is dominated on both axes — do not deploy.
- The `/no_think` directive is **ignored** by `qwen3:4b` over Ollama's OpenAI-compatible endpoint as of the version pinned in `start-ollama.ps1`. Treat thinking as always-on and design prompts accordingly.

## Constraints from the runtime

These are hard constraints on the Arc 140V box and dictate the design — see `MEMORY.md → OpenClaw Configuration`:

1. **VRAM ~7 GB, `MAX_LOADED_MODELS=1`.** Loading `reasoning` evicts `main` from VRAM and vice versa. Each agent switch costs a model-load (cold start: `llama-agent` ~26 s, `qwen3:4b` ~12 s observed in this session).
2. **OpenClaw minimum context = 16 000 tokens.** `qwen3:4b` ships with a 32 K context window, so this is fine.
3. **Stock Ollama (`v0.17.7`) on Windows.** Tool calling over `/v1/chat/completions` works for `qwen3:4b` and `llama-agent`; broken for `qwen3.5:9b` (do not use).
4. **`models.json` overrides `openclaw.json`.** Per-agent model selection MUST be set in `~/.openclaw/agents/<agent>/agent/models.json` — not in the global config.
5. **Agent exec runs PowerShell on Windows**, but llama-agent emits bash-style scripts. The reasoning agent should not be assigned execution-heavy tasks for this reason.

## Out of scope

- Replacing `main` for orchestration. `main` keeps its 14-tool surface and stays the cron-wired default.
- Adding GPU memory beyond what's already configured.
- Changing the Ollama backend (stock Ollama stays — IPEX-LLM is parked).
- Multi-model concurrent loading (would require a second device).

## 1. Agent layout

### Problem

OpenClaw currently runs five agents: `main`, `researcher`, `coder`, `auditor`, `tester`. None of them emit a reasoning trace, and `main` (llama-agent) is fast at tool calling but weak at multi-step analysis.

### Changes

- Create `~/.openclaw/agents/reasoning/` mirroring the `main` layout:
  - `agent/models.json` — selects `ollama/qwen3:4b`.
  - `AGENTS.md` — narrow role definition (see §3).
  - `TOOLS.md` — restricted tool surface (see §2).
  - `SOUL.md` — copy of `main`'s with the orchestration paragraphs removed.
  - `sessions/` — fresh.
  - `.learnings/` — empty seed.
- Register the agent in `~/.openclaw/openclaw.json` under `agents.reasoning` so `openclaw agent --agent reasoning` is available. Do not set it as default.
- Skills: enable only `prompt-guard`, `research-loop`, and `self-improvement`. Do **not** enable `aider-coder`, `scrapling-fetch`, or `dont-hack-me` — those are owned by other agents.

## 2. Tool surface

### Problem

The reasoning agent should not place trades, modify code, or launch subagents. It should read, search memory, fetch web docs, and write findings.

### Changes

- Allow tools: `read`, `web_search`, `web_fetch`, `memory_get`, `memory_search`, `write` (restricted to `~/.openclaw/workspace/projects/arbitrout/docs/research/` and `docs/audits/`).
- Deny tools: `exec`, `edit`, `sessions_spawn`, `subagents`, `process`, `cron`, `gateway`, plus the existing global denies (`message`, `browser`, `canvas`, `nodes`, `tts`, `agents_list`, `image`, `pdf`).
- The deny list mirrors the current `main` deny set with `exec`, `edit`, `sessions_spawn`, and `subagents` added — these are the tools whose misuse would let a hallucinating analytical agent change live state.

## 3. Role definition (`AGENTS.md`)

### Problem

Without a tight role, qwen3:4b's thinking trace will wander and burn tokens on irrelevant tangents.

### Changes

`AGENTS.md` for `reasoning` must enforce:

- **Inputs:** a single analytical question, optionally with a file pointer or memory key.
- **Outputs:** a markdown report under `docs/research/` or `docs/audits/`, with a `## Summary`, `## Reasoning`, and `## Recommendation` section. The reasoning section is allowed to be long; the summary is capped at 5 bullets.
- **Forbidden behaviors:** running shell commands, editing source files, spawning subagents, calling `web_fetch` more than 5 times per task, exceeding 4 K reasoning tokens.
- **Escalation:** if the question requires running code or editing files, the agent must `write` a "delegation needed" note and exit. `main` is responsible for picking that up.

## 4. Use cases

### Phase-1 use cases (manual invocation)

These are analytical tasks where the cost of a 12 s cold start + 7 s call is acceptable and the reasoning trace adds value:

1. **Journal post-mortem.** "Why did NO-side entries between $0.85 and $0.95 lose 80% of the time in the 2026-04-08 → 2026-04-13 window?" → reads `trade_journal_paper.json`, classifies losers, writes a report under `docs/audits/`.
2. **Calibration audit.** Given a set of resolved markets, report Brier score and reliability bins for each strategy. Cross-check against `eval_log.jsonl`.
3. **Synthetic strategy generation.** Given a political event cluster, propose a fee-adjusted EV synthetic and explain the dependency between legs. Output goes to `docs/research/synthetics/`.
4. **Spec drafting.** Given a freeform request from `main`, produce a well-structured `docs/specs/YYYY-MM-DD-*.md` for human review (the present spec is the reference template).

### Phase-2 use cases (delegation from `main`)

After `main` has run for a week with the reasoning agent available manually, wire `main` to delegate a narrow set of intents to it. Trigger phrases in the cron message:

- `"analyze ..."`, `"audit ..."`, `"why did ..."`, `"propose ..."`, `"draft a spec ..."`

`main`'s delegation flow (already supported via the existing `sessions_spawn` tool, which is enabled on `main` and `researcher`):

1. Detect the trigger phrase in the cron message.
2. Spawn an isolated session on the `reasoning` agent, passing the question and any required file pointers.
3. Wait up to 240 s for the report path.
4. Read the report, summarize in 3 bullets to its own log.
5. Forget the reasoning session.

## 5. Model swap cost

### Problem

Every switch between `main` and `reasoning` evicts the other from VRAM. If we delegate carelessly, the cron loop will spend most of its time loading models.

### Changes

- Batch reasoning tasks. Phase-2 delegation must accumulate at least 3 pending analytical questions before swapping out `main`.
- Add a 5-minute reasoning quiet window after a swap-back to `main`, to amortize the load cost.
- Track swap counts and average load times in a new metric file at `~/.openclaw/metrics/agent_swaps.jsonl` (one JSON line per swap with `{ts, from, to, load_ms, prompt_ms_first_token}`). Surface in the gateway log.
- Cron-wire `reasoning` only on a slow cadence (every 6 h) for routine audits. Manual one-shot use bypasses the cron.

## 6. Test plan

Each item below is a manual gate before flipping the corresponding phase live.

### Pre-flight (no funds, no live state)

- [ ] `ollama pull qwen3:4b` succeeds and the model loads on first request.
- [ ] `~/.openclaw/agents/reasoning/agent/models.json` resolves to `ollama/qwen3:4b` via `openclaw agent --agent reasoning --message 'identify yourself' --json`.
- [ ] Tool-call smoke test from `bench-tool-models.ps1` shows `pass=True` for `qwen3:4b`.
- [ ] Deny list is enforced: `openclaw agent --agent reasoning --message 'run ls'` must refuse and not invoke `exec`.

### Phase-1 acceptance

- [ ] One journal-post-mortem task completes end-to-end and writes a report under `docs/audits/`.
- [ ] One spec-drafting task completes and produces a markdown file matching the structure of `2026-04-10-true-arb-and-kelly-hardening.md`.
- [ ] Reasoning trace is bounded — total completion tokens ≤ 4 000 per task on a 6-month average.
- [ ] No `exec` / `edit` calls in the agent's session history.

### Phase-2 acceptance

- [ ] `main` correctly detects the 5 trigger phrases on a fixture set of 20 prompts (≥ 18/20).
- [ ] Average swap-load latency on the metric file ≤ 30 s over 24 h.
- [ ] No reasoning task starves the trading loop — `auto_trader` cycle latency p95 unchanged ± 10 % over a 24 h window with reasoning enabled.

## 7. Rollback

- Disable by removing `agents.reasoning` from `~/.openclaw/openclaw.json` and removing the trigger-phrase delegation block from `main`'s `AGENTS.md`. The model file stays — uninstall via `ollama rm qwen3:4b` only if VRAM is needed for something else.
- All artefacts written by the reasoning agent live under `docs/research/` and `docs/audits/`. Removing the agent does not remove its outputs.

## 8. Open questions

- Should the reasoning agent get read-only access to the eval logger (`eval_log.jsonl`) or only via a curated snapshot? Curated snapshot is safer; read-only is more useful. **Default: curated snapshot for Phase-1, revisit before Phase-2.**
- Should we wire the reasoning agent into the live trading kill-switch decision path? **No — Phase-1 explicitly forbids it. Revisit only after the live rollout has its own observability story.**
- Is the 4 K reasoning-token budget too tight for political-synthetic generation? Likely. Carve out a per-task override that requires explicit invocation (`--max-reasoning-tokens`).

## 9. Out-of-band note

This spec is OpenClaw-side configuration plus arbitrout-side documentation. There are no source-code changes to arbitrout in scope here. Any code change discovered during implementation gets its own follow-up spec — do not bundle.
