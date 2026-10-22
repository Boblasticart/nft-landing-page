# Free-tier AI orchestrator

Runs on your machine and splits any build task ("build me X") across free-tier AI APIs while tracking each one's daily limits. Pure Python 3.9+ standard library, so there's nothing to install.

## Quick start

```bash
cp .env.example .env          # add the API keys you have; leave the rest blank
python -m orchestrator agents # shows which agents are ready
python -m orchestrator run --dry-run "Portfolio site with dark mode"   # offline test, no API calls
python -m orchestrator run "Portfolio site with dark mode"             # real run
python -m orchestrator run                                             # asks you what to build
```

The generated project lands in `output/<project-name>/`.

## Watching it

- **Terminal:** every routing decision, reroute, and pause is logged live.
- `python -m orchestrator status [RUN_ID]` shows a usage table and per-subtask progress.
- `python -m orchestrator dashboard` starts a live page at http://127.0.0.1:8765 with quota meters, subtask states, and the log. It only listens on localhost.

## How it works

1. **Plan:** a `planning` agent breaks the task into subtasks, each with one capability: `design`, `copy`, `code`, or `debug`. If no planner has quota, or its output is unusable, a built-in website template plan is used.
2. **Route:** each subtask goes to the highest-priority agent that has the capability and enough quota left. Quota means daily requests, daily tokens (estimated before the call), the per-minute window, and any cooldown. If no exact match is available, `capability_fallbacks` allows borrowing, e.g. a `code` agent can write the design spec.
3. **Reroute:** errors are handled like this:
   - HTTP 429 cools the agent down; if the provider says the daily quota is gone, the agent is marked exhausted for the day.
   - 401/403 disables the agent for the session.
   - Other errors try the next agent.
   - A `code` answer with no files is retried on a different agent.
4. **Assemble:** agents return files as `=== FILE: path ===` blocks, which are written into the project folder. Paths outside the project folder are refused. Design and copy notes are passed on as context to later subtasks.
5. **Degrade:** when every agent for a capability is out, that subtask is paused and anything depending on it is blocked. You get a terminal alert with three options:
   - resume after the daily reset with `python -m orchestrator resume RUN_ID`
   - add a provider key
   - enable the local Ollama fallback

## Configuring agents

Edit `orchestrator/agents.json`. **The limits there are placeholders.** Check your real free-tier limits in each provider's console, because they change often. Fields:

| field | meaning |
|---|---|
| `type` | `openai` (any OpenAI-compatible endpoint, including Ollama/LM Studio), `gemini` |
| `capabilities` | subset of `planning, design, copy, code, debug` |
| `priority` | lower is tried first |
| `limits` | `requests_per_day`, `requests_per_minute`, `tokens_per_day`; `null` means untracked |
| `api_key_env` | env var holding the key; `null` for local models |
| `enabled` | set `false` to skip an agent |

**Local fallback:** install [Ollama](https://ollama.com), run `ollama pull qwen2.5-coder:7b`, and set `"enabled": true` on `ollama-local`. It has the lowest priority, so it only runs once the free APIs are used up.

## Local state

Everything is kept in `.orchestrator/` in the directory you run from:

- `state.json` holds the daily counters, which reset at local midnight.
- `runs/<id>/` holds each run's plan, statuses, notes, and events.
- `orchestrator.log` is the full log.

Prompts go only to the provider endpoints you configure. Subtasks currently run one at a time, which keeps per-minute limits predictable.
