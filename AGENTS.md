# Agent Guidance

## Project Snapshot

- `mimi3` is a Claw instance account manager + proxy-skill deployer. It manages a pool of Xiaomi AI Studio accounts and deploys `cloudflared + Caddy` proxy skills into their Claw instances, making them reachable via Cloudflare Tunnel public URLs.
- There is no relay gateway layer — users call the tunnel URL directly. Each Claw instance is an independent proxy endpoint.
- The runtime entrypoint is `src/run_manager.py` (`plan` for dry-run, `run` for continuous operation).
- Project managed via `uv` (see `pyproject.toml`, `uv.lock`).

## Important Paths

- `src/run_manager.py`: CLI entrypoint (`plan` / `run` / `deploy` / `status`)
- `src/deploy_one.py`: single-account deploy script
- `src/account_manager.py`: multi-account orchestration, reconcile, tick loop
- `src/account_store.py`: account state persistence (JSON files per account)
- `src/scheduler.py`: deployment scheduling (handoff, stagger, cooldown)
- `src/health_monitor.py`: periodic connectivity checks per active account
- `src/claw_client.py`: NativeClawClient — WS connect/send_message/get_instance_status
- `src/claw_deployer.py`: ClawDeployer — full deploy flow (status → connect → inject → verify)
- `src/deploy_errors.py`: error classification (classify_reply, error types)
- `src/prompt_store.py`: prompt template management with `{{VAR}}` substitution
- `src/config.py`: two-layer config loader (os.getenv + data/config/config.json; root config.json is legacy fallback)
- `src/tunnel_health.py`: tunnel endpoint health probe
- `data/prompts/templates.json`: inject prompt templates (placeholders only, no secrets)
- `data/prompts/_gen_templates.py`: prompt generator script
- `test/test_inject.py`: standalone injection test
- `webui/server.py`: FastAPI backend serving REST API + static frontend
- `webui/static/index.html`: operator dashboard HTML (Tailwind + Fira fonts)
- `webui/static/app.js`: dashboard JS (auto-poll, actions, toasts, confirm dialogs)
- `webui/static/style.css`: dashboard custom styles (dark theme, badges, health dots)
- `data/`: runtime state (creds, state, logs, config) — gitignored except prompt templates; root `.env` and legacy `config.json` are also gitignored

## Key Conventions

- **Config layering**: `.env` for secrets (os.getenv), `data/config/config.json` for operational params
- **Auth layering**: user → Claw (PROXY_API_KEY via Caddy), Claw → MiMo (MIMO_API_KEY upstream header)
- **Cooldown**: 24h rolling cooldown per account after each deploy
- **Prompt templates**: `{{VAR}}` placeholders in `templates.json`, substituted at runtime from `.env` + `data/config/config.json`
- **Error classification**: `classify_reply()` checks success markers BEFORE refused markers (strong markers only, no bare emoji)
- **Account states**: idle → needs_deploy → deploying → active → cooldown → relogin_needed → disabled
- **All Claw instances share one tunnel URL** — Cloudflare load-balances across replicas. No per-account routing.

## Trellis Workflow

- Before starting any non-trivial change, inspect the current Trellis task with
  `python ./.trellis/scripts/task.py current`.
- If the user explicitly says to enter Trellis mode/task, use the Trellis CLI
  immediately before reading or editing implementation files.
- If the request is a new independent goal with its own acceptance criteria
  (for example a new feature version, frontend redesign, broad test coverage,
  state-model change, or workflow-rule update), proactively ask whether to
  create/switch to a dedicated Trellis task; if the user has already clearly
  asked for Trellis workflow, create or switch to the matching task instead of
  continuing under an unrelated current task.
- If the current Trellis task does not match the user's newest request, pause
  implementation and ask whether to enter an existing task or create a new one.
- Before editing code for a Trellis-scoped task, read that task's `prd.md`,
  `design.md` when present, `implement.md` when present, run
  `python ./.trellis/scripts/get_context.py --mode packages`, then start the
  task with `python ./.trellis/scripts/task.py start <task-dir>`.
- Keep task scope honest: small follow-up fixes may stay in the active task,
  but separate deliverables should get a separate Trellis task or an explicit
  user confirmation to reuse the current one.

## Running

```bash
uv run --env-file .env python src/run_manager.py plan    # dry-run
uv run --env-file .env python src/run_manager.py run     # continuous operation
uv run --env-file .env python src/run_manager.py status  # current state
uv run --env-file .env python src/deploy_one.py data/creds/user_<uid>.json deploy.v1.standard  # single deploy
uv run --env-file .env python test/test_inject.py <creds_file>        # test injection
uv run --env-file .env uvicorn webui.server:app --host 127.0.0.1 --port 8358  # WebUI dashboard
uv run python -m compileall -q src webui test                         # syntax check
```

## Coding Guidelines

- Python 3.12, stdlib-heavy (asyncio, dataclasses, json, re, logging)
- Prefer `uv` for dependency management; use pip as fallback
- Keep `config.py` as the single config resolution point
- Async I/O for all network paths; no blocking calls in critical paths
- Preserve `{{VAR}}` substitution pattern for prompt templates
- Log compact summaries, not full credentials or prompt text
- Don't commit `data/` runtime artifacts, `.env`, `skills-lock.json`, `.claude/`, `.trellis/`

## Testing

```bash
uv run python -m compileall -q src test      # syntax check
uv run --env-file .env python test/test_inject.py <creds_file>
```

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **mimi3** (1097 symbols, 2967 relationships, 80 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "main"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit changes without running `detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/mimi3/context` | Codebase overview, check index freshness |
| `gitnexus://repo/mimi3/clusters` | All functional areas |
| `gitnexus://repo/mimi3/processes` | All execution flows |
| `gitnexus://repo/mimi3/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
```

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **mimi3** (419 symbols, 835 relationships, 36 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "main"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit changes without running `detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/mimi3/context` | Codebase overview, check index freshness |
| `gitnexus://repo/mimi3/clusters` | All functional areas |
| `gitnexus://repo/mimi3/processes` | All execution flows |
| `gitnexus://repo/mimi3/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
