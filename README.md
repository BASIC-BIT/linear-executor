# linear-executor

**Empower your chatbot to actually ship code.** A bridge between any chat application (Claude.ai, ChatGPT, Telegram, Discord, AnythingLLM, your custom bot) and your favourite AI coding agent (Claude Code, opencode, codex, gemini, forge).

You chat. The chatbot writes a Linear ticket. The executor picks it up, spawns the coding agent in a worktree, runs it headless with your full tooling (MCP servers, skills, hooks, environment), captures the result, and posts it back as Linear comments. Your chatbot reads those comments and tells you what happened — or you read them yourself in the Linear UI.

## Why this exists — supercharge your chatbot

Your chatbot doesn't have to be the agent. **Your chatbot just has to know to write a Linear ticket.** Suddenly it can:

- Modify code in a real repo (with worktrees, branches, commits, pushes)
- Pull data via any of your MCP servers (GitHub, Linear, Stripe, Supabase, your custom ones)
- Hit any HTTP API the host can reach — image generation, transcription, payments, internal tooling — whatever your coding CLI can curl
- Run shell commands, call APIs, do file conversions — anything your coding CLI can do
- Attach generated files back to the ticket so the chatbot can read them

That's the supercharge: a casual conversational interface (Telegram, ChatGPT desktop, Claude.ai, your own bot) suddenly inherits the full toolbox of your coding agent setup.

**Caveat: the chatbot has to learn *when* to escalate to a Linear ticket** vs answering inline. This is a system-prompt / skill / tool-decision-instruction problem on the chatbot side — not solved by Linear-Executor itself. Typical pattern: tell the chatbot in its system prompt *"when the user asks for code changes, file generations, or anything multi-step, create a Linear ticket via the Linear MCP and watch the comments for the result."* Each chatbot platform has its own way of teaching this.

Modern chatbots are great at conversation. AI coding agents are great at making real code changes. The gap between them — *"please go change this codebase and tell me when you're done"* — is usually papered over with manual copy-paste, screenshots, or building a custom integration per chatbot.

Linear is a perfect transport layer:
- Every modern chatbot can talk to Linear (via the Linear MCP, or by simple API calls).
- Tickets are persistent, threaded, comment-able, and have built-in status workflows.
- Linear's webhooks fire reliably on status changes.

So instead of building chatbot ↔ coding-agent integrations N×M, you build it once: **Linear in the middle, executor on the side, your existing coding CLI doing the actual work.**

```
                                                        ┌────────────────────────────┐
   You ────chat────▶  Chatbot ───MCP/API─▶  Linear ───webhook──▶  Linear-Executor
                       (Claude.ai,            (ticket             │  Verify HMAC
                        ChatGPT,               + status            │  Enqueue job
                        Telegram,              + comments)         │  Pick CLI from label
                        AnythingLLM,                               │  Spawn coding agent:
                        custom)                                    │    claude / opencode /
                                                                   │    codex / gemini / …
                                                                   │  with your full MCP +
                                                                   │  skills + rules + .env
                                                                   │  Capture output
                                                                   │  Edit lifecycle comment
                                                                   │  Post detailed result
                                                                   │  Attach generated files
                                                                   │  Flip ticket state
                                                                   ▼
                                                            ✅ Done — chatbot reads
                                                              the comment, tells you
```

## Features

### Use any chat as an interface
Anything that can write to Linear can drive a coding session. **No per-chatbot integration code needed.** Tested with Claude.ai (via Linear MCP), ChatGPT (via Linear MCP), AnythingLLM (via API), Telegram bots, and direct manual ticket creation in Linear UI itself.

### Use your favourite coding agent
Pick the agent per ticket via a Linear label `cli:<name>`:

| Label | Agent | Position |
|---|---|---|
| _(none)_ | Claude Code | default — most reliable, best tool-use |
| `cli:claude` | Claude Code | explicit |
| `cli:opencode` | opencode | open-weights, privacy-first, cost-optimized |
| `cli:codex` | OpenAI Codex CLI | OpenAI ecosystem |
| `cli:gemini` | Google Gemini CLI | Google ecosystem, large context |
| `cli:forge` | Forge | multi-agent harness |

Per-ticket model override with a second `model:<id>` label. Registry-driven (`app/cli_registry.py`) — adding a backend is one line plus a label.

### Full coding-agent context, not a sandbox
The agent runs in your real dev environment with **all your tooling preserved**:
- All your **MCP servers** (Linear, GitHub, Stripe, Supabase, your custom ones — full access)
- All your **skills** (`~/.claude/skills/`, `~/.config/opencode/...`)
- All your **rules** (`~/.claude/rules/*.md` auto-loaded)
- All your **hooks** (`~/.claude/settings.json`)
- All your **environment** (full `~/cc-dev/.env` injected: API keys, tokens, project context)
- All your **git history**, **SSH keys**, **MCP-tool sessions**

Whatever your coding CLI can do interactively on your machine, it can do via Linear ticket.

### Live status comments — see progress in real time
A single status comment per job, edited in place:

- ⏳ **Queued** — webhook received, waiting for worker
- 🏃 **Running** — claimed by worker, with queue-wait time
- ✅ **Done** — finished, with runtime, points to the detailed result comment
- ❌ **Failed** — exhausted retries
- 🔁 **Retrying** — intermediate failure, will retry

Your chatbot can poll the comment to know when the work is done. Or you watch the lifecycle update in the Linear UI like a CI build.

### File attachments — outputs come back to you
Files generated during the run get auto-attached to the Linear ticket via Linear's `fileUpload` flow. Generated images, PDFs, datasets, refactored code diffs — all there for you to download. No "where did the output go?" hunt.

### Two execution modes
**Stage 1 / Stage 2 coding flow** — for substantive code changes:
- Auto git-worktree per ticket, branch named `ticket/TES-XXX`
- Auto-commit on completion
- When you flip the ticket to **Done**, Stage 2 fires: merges branch into main, pushes, cleans up worktree

**Ad-hoc proxy mode** — for quick chat-style tasks (Q&A, lookups, image generation, file conversions):
- No git, no branches
- Runs the agent in `proxy-outputs/<ticket>/` for a clean workspace
- Posts the result back as a comment
- Great for "look up X for me" or "draft me a teaser image" kind of asks
- Opt-in: create a Linear project (any name) and set its UUID in `.env` as `LINEAR_PROXY_PROJECT_ID`. Tickets in that project skip Stage 1/2 and run via the proxy flow.

### Trigger from anywhere
- **From your chatbot** via Linear MCP (the original use case)
- **From the Linear UI directly** — manually create a ticket, flip status, watch it run (great for ad-hoc work, debugging, demos)
- **From CLI** via `linear-cli` or curl + Linear's GraphQL API
- **From a cron** — schedule recurring tasks as ticket creates
- **From other systems** — anything that can hit Linear's API

### Cancel from the UI
Flip the ticket to **Stop AI** state → executor sends SIGTERM to the running subprocess, posts a cancellation note, marks queue jobs cancelled. No orphan processes, no half-applied changes you didn't want.

### Batch mode
Tickets in the *AI Batch* state get queued and processed in a separate lane, suitable for parallel non-blocking workloads (e.g. bulk content generation, research-batch tasks).

### Resilience
- HMAC-SHA256 signed webhooks (constant-time verify, replay-resistant)
- SQLite queue with WAL mode — survives restarts
- Retry with backoff before final-failure reset
- Boot-time recovery: jobs left in `running` after a hard restart are automatically returned to the queue
- 149 tests covering signature, queue, dispatch, cancel, batch, attachments, orchestrator paths

## How it differs from OpenClaw, Hermes Agent, ClaudeClaw

These projects all sit somewhere on the "let me chat with an agent that does real things" axis but at very different points. Honest comparison:

| | **Linear-Executor** | **ClaudeClaw** | **OpenClaw** | **Hermes Agent** |
|---|---|---|---|---|
| **What is it?** | Linear-driven dispatcher | Lightweight Claude Code daemon | Full personal AI runtime | Full self-improving agent runtime |
| **Wraps an existing coding CLI?** | Yes, any (`claude` / `opencode` / `codex` / `gemini` / `forge`) | Yes — Claude Code specifically | No, has its own agent loop | No, has its own agent loop |
| **LLM backends** | Whatever your CLI supports | Claude Code subscription + GLM fallback | Local + cloud (Anthropic, OpenAI, Google) | Pluggable: Nous Portal, OpenRouter, OpenAI, local via MCP |
| **Memory / skill ecosystem** | Inherits your CLI's MCP + skills + rules | Inherits Claude Code's MCP + skills + `CLAUDE.md` | Own skill / plugin community | Own skill system with **self-improving learning loop** |
| **Chat interface?** | **Linear UI / any Linear-MCP-aware chatbot** | **Telegram + Discord** built-in (voice, threads, slash commands) | Telegram, WhatsApp, Discord built-in | Telegram, Discord, Slack, WhatsApp, Signal, Email, CLI built-in |
| **Where does it run?** | Local or VPS — anywhere reachable by HTTPS | Local or VPS — wherever Claude Code runs | Local or VPS — your choice | Local or VPS — your choice |
| **Sandboxing / isolation** | Per-ticket git-worktree | Per-folder Claude Code session | Global by default | Multiple backends: Docker, SSH, Daytona, Singularity, Modal |
| **Coding-specific?** | Yes, primary use-case | Yes (uses Claude Code) | No, general PA | No, general agent |
| **Setup effort** | Install + Linear webhook (~5 min) | `claude plugin install claudeclaw` (~5 min) | Full runtime install + config | Full runtime install + sandbox config |
| **Codebase size** | ~3k LOC | "lightweight" (TypeScript) | ~600k+ LOC | ~tens of k LOC |
| **Best for** | "Drive coding work via Linear tickets from any chatbot" | "Make Claude Code reachable on Telegram/Discord 24/7" | "I want one big personal AI assistant" | "I want a server-side agent that learns and grows" |

**Tl;dr:**
- **Linear-Executor + ClaudeClaw** are *thin wrappers around your coding CLI* — different transport (Linear tickets vs Telegram/Discord chats), same idea: "your existing Claude Code, reachable from somewhere else, with all its tools intact."
- **OpenClaw + Hermes** are *full agent runtimes* with their own loops, skills, memory, and pluggable LLM backends. Bigger product, bigger surface area, more capability out of the box.

They compose well: a Hermes Agent or OpenClaw instance can write Linear tickets via the Linear MCP → Linear-Executor picks them up → spawns Claude Code → result back. Or a ClaudeClaw daemon on Telegram receives "fix the auth bug", creates a Linear ticket, Linear-Executor processes it. Pick the layers you want.

If you already own and trust a Claude Code / opencode / codex setup with all your MCPs, skills, and rules, **Linear-Executor is the smallest possible glue layer** to expose it through Linear-aware chat.

### Honorable mention: ClaudeClaw

[github.com/moazbuilds/claudeclaw](https://github.com/moazbuilds/claudeclaw) — *"A lightweight, open-source OpenClaw version built into your Claude Code."* MIT-licensed, ~1k stars, actively developed. If you're reading this README and your need is *"reach my Claude Code over Telegram or Discord"* rather than *"drive coding work via Linear"*, ClaudeClaw is probably what you want. Same philosophy as Linear-Executor (wrap Claude Code, don't replace it), different chat-medium. Recommended.

## Quick start (your own Linear workspace)

```bash
git clone https://github.com/miraculix95/linear-executor
cd linear-executor
uv venv
uv pip install -e ".[dev]"
cp .env.example .env
#  → set LINEAR_WEBHOOK_SECRET, LINEAR_API_KEY, LINEAR_TEAM_ID
uv run pytest -v
uv run uvicorn app.main:app --host 127.0.0.1 --port 8123
```

Expose port 8123 over HTTPS (Caddy / Cloudflare Tunnel / ngrok), then in Linear:

> Settings → Administration → API → Webhooks → New webhook
> - URL: `https://<your-host>/webhook`
> - Resource types: `Issues`
> - Copy signing secret → paste into `.env` as `LINEAR_WEBHOOK_SECRET`

Restart uvicorn. Make a ticket, flip to **AI Implementation** (or your trigger state), watch the lifecycle comment appear.

To use a non-Claude backend: install the CLI on the same machine (`opencode`, `codex`, `gemini`, `forge`), make sure it's authenticated, then add `cli:<name>` as a Linear label on the ticket. Per-CLI auth and gotcha notes: `docs/backends.md` (TODO — currently only Claude Code + opencode are smoke-tested end-to-end).

### Optional: enable ad-hoc proxy mode

If you want a project where tickets get a quick Q&A-style answer (no worktree, no merge, status straight to Done), pick or create a Linear project for it, copy its UUID, and set in `.env`:

```bash
LINEAR_PROXY_PROJECT_ID=<your-project-uuid>
```

Restart uvicorn. Tickets in that project will now use the lightweight proxy flow; tickets in any other project keep the default Stage 1 / Stage 2 build-with-merge flow. Leaving the variable unset disables proxy mode entirely.

To find the UUID, open the project in Linear and run:

```bash
curl -sS https://api.linear.app/graphql -H "Authorization: $LINEAR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"{projects(first:50){nodes{id name}}}"}' | jq '.data.projects.nodes[]'
```

## Configuration via Linear labels

| Label | What it does |
|---|---|
| `cli:claude` / `cli:opencode` / `cli:codex` / `cli:gemini` / `cli:forge` | choose the coding CLI for this ticket |
| `model:<id>` | override the registry default model (e.g. `model:opencode-go/glm-5.1`) |

Group both as Linear label groups for a cleaner picker (mutually exclusive).

## Repo layout

```
app/                              FastAPI app, worker, orchestrator
  main.py                         /health + /webhook (HMAC verify, enqueue, lifecycle comment seed)
  worker.py                       polling worker, claim → dispatch → status updates
  orchestrator.py                 stage1 / stage2 / proxy / batch flows
  queue.py                        SQLite queue with status_comment_id column
  linear_api.py                   commentCreate/Update + fileUpload + state-set
  cli_registry.py                 cli:<name> + model:<id> label resolution
  cancel.py                       Stop-AI status → SIGTERM the running subprocess
  job_registry.py                 in-memory map ticket → Popen
  runner.py                       subprocess launcher with timeout + cancel hook
  signature.py                    HMAC-SHA256 verify (timing-safe)

tests/                            149 tests
scripts/                          companion tooling (maintainer-specific, not part of the public surface)
logs/                             rolling logs
state/jobs.db                     SQLite queue
proxy-outputs/<ticket-id>/        ad-hoc proxy mode work dirs
originals/                        scraped Linear webhook docs (reference)
```

## Phase history (commits)

- **Phase 1** (TES-466): HMAC webhook receiver POC.
- **Phase 2-9**: orchestration, queue, cancel-via-status, ad-hoc proxy mode, retries, attachments via GCS.
- **Phase 9.1** (TES-615): stage1 auto-attach + content-type fix.
- **Phase 10** (TES-612): AI Batch mode.
- **TES-646**: CLI-agnostic via `cli:<name>` Linear labels.
- **Phase 10.2**: live status-comment lifecycle (queued → running → done).
- **Phase 10.3**: opencode `--model` flag, shared dev-workspace `.env` loading for subprocess CLIs.
- **Phase 10.4**: filter lifecycle comments out of follow-up context, per-ticket `model:<id>` override.

## Status

Production-running on the maintainer's dev-server, processing live tickets daily across `claude` and `opencode` backends. **Codex / Gemini / Forge backends are in the registry but not yet smoke-tested end-to-end** — see "Backend smoke-tests" issue. **Repo private** until those backends round-trip.

If you try it on your own setup and run into the gaps, please open an issue. Especially interested in feedback from people running it as the bridge for their own chatbot stack.

## Security & sandboxing

### What's enforced today

- **HMAC-SHA256 signature** on every webhook (constant-time compare). Linear's signing secret is the only externally-visible auth.
- `.env` files chmod 600.
- **Per-ticket git-worktree** isolation — accidental commits stay on `ticket/TES-XXX` branches, not main, until Stage 2 explicitly merges them.
- The coding CLI runs as the host user with **full access to the dev environment**: your code, your `.env`, your SSH keys, all MCP servers, all skills, all rules.

**Implicit threat model: anyone with Linear write-access to the configured workspace can execute arbitrary code on the host machine.** Treat Linear write-access as "trusted user with shell" for this server.

### What's *not* enforced today (be aware)

- **No system-prompt guard against destructive commands.** A misbehaving (or jailbroken) agent could `rm -rf`, force-push, modify `.env`, exfiltrate secrets. You rely on:
  - The coding CLI's own training/alignment (Claude Code is generally well-behaved)
  - Your prompt being clear about scope
  - Worktree isolation absorbing accidental file edits
- The default `--dangerously-skip-permissions` flag on Claude Code means **no human-in-the-loop confirmation per shell command** — fast but trust-heavy. Other CLIs have similar non-interactive modes.
- No sandboxing of network egress, filesystem writes, or process spawn.

### Hardening you can do today (no code changes)

- **Lock down the webhook endpoint to Linear's source IPs** at your reverse-proxy. Linear publishes them. Cuts off random-Internet HMAC-bruteforce attempts.
- **Run the executor on a dedicated host/VPS**, not your daily-driver dev machine. Limits blast radius of an agent gone rogue.
- **Trim `.env`** to only the keys the executor actually needs. Move sensitive keys (Stripe live, Hostinger, etc.) to a separate file the spawned CLI doesn't see by `load_dotenv`.
- **Enable Linear 2FA + audit workspace write-access.** This is the actual auth boundary.
- **Review Stage 2 merges** before flipping tickets to Done — the diff is in the run-comment.

### Hardening that's not yet built (TODO — contributions welcome)

These are the natural next steps if/when the attack surface starts mattering:

1. **System-prompt safety injection** — prepend a configurable safety preamble to every prompt (`"You must never run rm -rf, never force-push, never modify .env, never exfiltrate secrets..."`). Per-CLI mechanism varies. **Low effort, ~30 LOC.** Easy first contribution. Mitigates most casual-jailbreak risk; a determined attacker can override.
2. **Per-CLI safety mode** — replace `--dangerously-skip-permissions` with `--permission-mode plan` (Claude Code) or equivalent. Trade off: each shell command needs a human-approval webhook bounce. Good for high-trust-required scenarios.
3. **Docker sandbox per ticket** — run the coding CLI in a container with only the worktree mounted, no host network egress except the model API + Linear API. Medium effort, requires per-CLI Dockerfile. Hard guarantee against `rm -rf /`.
4. **MCP allowlist per ticket** — label-driven, e.g. `mcp:linear,github` exposes only those MCPs to the subprocess. Limits credential-scope per task.
5. **Capability flags via labels** — `cap:read-only` mounts the workspace read-only, `cap:no-network` blocks egress at iptables level, `cap:no-secrets` strips `.env` from the env. Composable per-ticket security posture.
6. **Output-diff regex screen** before commit — block patterns like `rm -rf /`, `aws s3 rm`, `kubectl delete`. Detects, doesn't always prevent.
7. **Idle-time agent caps** — kill subprocess after N minutes / M shell commands / X tokens. Prevents runaway loops.

If you have a strong opinion on which of these to build first, open an issue. The security story is honest at the moment — *trust the CLI's alignment, treat Linear write-access as shell-access* — and there's deliberate room for hardening when use-cases demand it.

## License

MIT (planned — repo currently private during pre-public smoke-testing).
