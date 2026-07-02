# Agent Guidance

This guidance applies to the entire repository.

## Project Snapshot

- `mimi3` / `mimo2api` is a Python 3.12 FastAPI gateway that exposes OpenAI-compatible relay endpoints, a WebUI control panel, WebSocket bridge management, lifecycle monitoring, Cloudflare Tunnel supervision, metrics persistence, and AI Studio web-chat proxy routes.
- The runtime entrypoint is `main.py`. It loads `.env`, resolves runtime configuration, configures logging, imports the FastAPI app from `mimo2api.web_service`, and starts Uvicorn.
- The project is intentionally small and direct. Prefer focused changes inside existing modules over broad rewrites or new framework layers.

## Important Paths

- `main.py`: process entrypoint, event loop policy, logging setup, Uvicorn launch.
- `mimo2api/web_service.py`: FastAPI app, API routes, WebSocket gateway behavior, session/API integration.
- `mimo2api/webui.html`: single-file WebUI. Keep API contracts synchronized with backend changes.
- `mimo2api/ui_router.py`: WebUI routing helpers.
- `mimo2api/manager.py`: account orchestration, bridge injection, node waiting, lifecycle-related account behavior.
- `mimo2api/bridge.py`: bridge-side WebSocket client behavior, node identity hello payloads, heartbeat behavior.
- `mimo2api/gateway_state.py`: shared in-memory WebSocket/node state.
- `mimo2api/gateway_health.py`: remote gateway stats parsing and node presence helpers.
- `mimo2api/lifecycle_monitor.py`: lifecycle classification and bridge presence resolution.
- `mimo2api/runtime_config.py`: merged environment/runtime configuration, including `data/runtime_config.json`.
- `mimo2api/tunnel_supervisor.py`: Cloudflare Tunnel process supervision.
- `mimo2api/metrics_store.py`: metrics snapshot and SQLite persistence.
- `mimo2api/web_chat_proxy.py`: AI Studio web-chat HTTP/WebSocket proxy routes.
- `mimo2api/auth.py`: WebUI/API authentication helpers.
- `mimo2api/logging_utils.py`: compact structured log helpers and library log-level control.
- `tests/`: unittest-based focused regression tests.
- `users/`, `logs/`, `data/`: runtime state. Treat contents as local artifacts unless the user explicitly asks otherwise.

## Local Commands

Install runtime dependencies:

```bash
pip install -r requirements.txt
```

Run the gateway:

```bash
python main.py
```

Run with Docker:

```bash
cp env.example .env
docker compose up -d --build
```

Run tests:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

Syntax-check the main code paths:

```bash
python -m compileall -q main.py mimo2api tests
```

If the local `.venv` is stale or points to a missing interpreter, recreate it instead of relying on it.

## Configuration Rules

- Copy `env.example` to `.env` for local runs. Never commit real `.env` values.
- `WS_TUNNEL_URL` is the bridge WebSocket URL used by Claw nodes. When explicitly set by environment, treat it as a locked manual value.
- Runtime WebUI configuration should go through `mimo2api/runtime_config.py` and `data/runtime_config.json`; do not mutate `.env` from application code.
- Docker persists runtime files through `./users`, `./logs`, and `./data`.
- Keep `SERVER_HOST`, `SERVER_PORT`, metrics paths, process lock path, WebUI auth settings, lifecycle settings, and tunnel settings compatible between `env.example`, Docker, and runtime config code.

## Coding Guidelines

- Preserve the existing FastAPI/Uvicorn architecture. Use `httpx`, `websockets`, standard library helpers, and existing local utilities before adding dependencies.
- Keep endpoint compatibility for `/v1/chat/completions`, `/v1/responses`, `/anthropic/v1/messages`, `/api/stats`, lifecycle APIs, WebUI APIs, and `/api/web-chat/<uid>/...` proxy paths.
- Maintain WebSocket node identity behavior. Bridge clients should send a `hello` payload and periodic `heartbeat` messages containing the node UID; gateway state should map node UID to the active WebSocket.
- Do not reintroduce repeated bridge injection when the gateway can only see unknown remote nodes. Preserve the ambiguity handling in manager/lifecycle code.
- Avoid blocking the event loop in request handlers or WebSocket paths. Use async clients and timeouts for network work.
- Keep logging compact and safe. Do not log full AI replies, reset prompts, credentials, cookies, session tokens, or large upstream payloads at INFO. Use `log_event` and `compact_text` for structured summaries.
- When editing `webui.html`, keep controls practical and dense. Avoid adding explanatory marketing text; this is an operational control panel.
- Preserve UTF-8 file handling. Some Chinese text may display incorrectly in non-UTF-8 terminals; do not "fix" existing text encoding unless the task is explicitly about encoding.

## Testing Expectations

- Add or update focused `unittest` tests under `tests/` for behavior changes.
- Prefer direct unit tests for parsers, lifecycle decisions, auth helpers, runtime config resolution, and manager decision branches.
- For WebSocket changes, test state transitions in `gateway_state`, `web_service` binding helpers, and manager wait/injection behavior.
- For logging changes, test that sensitive or long text is compacted and not emitted directly.
- Run `python -m unittest discover -s tests -p "test_*.py"` before claiming behavior is fixed. If dependencies or the interpreter are unavailable, report that clearly and at least run `compileall`.

## Runtime Artifacts And Secrets

- Do not commit `.env`, account credentials under `users/`, generated logs, SQLite metrics databases, snapshots, process locks, or `data/runtime_config.json`.
- Treat `users/user_<uid>.json` files as sensitive local credentials.
- Treat cookies, bearer keys, WebUI passwords, WebUI session secrets, Cloudflare tokens, and upstream AI Studio values as secrets.
- Do not delete runtime data unless the user specifically asks for cleanup and confirms the target paths.

## Git And Change Hygiene

- Check `git status --short --branch` before editing.
- The worktree may already contain user changes. Do not revert, overwrite, or reformat unrelated edits.
- Keep diffs scoped. Avoid broad formatting churn, especially in `webui.html` and large orchestration modules.
- If changing public behavior, update `README.md` or `env.example` when needed.
- If changing Docker/runtime paths, keep `Dockerfile`, `docker-compose.yml`, `env.example`, and docs aligned.
- For goal-driven work, update `F:\AI\my-obsidian\mimi3\mimi3自建.md` after verification with task status, acceptance notes, test commands, and remaining risks.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **mimi3** (1038 symbols, 4129 relationships, 86 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

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
