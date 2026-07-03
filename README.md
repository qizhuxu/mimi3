# mimi3

Proxy-skill deployer & account manager for Xiaomi AI Studio Claw instances.

Deploys [cloudflared + Caddy proxy skill](https://github.com/qizhuxu/cf-tunnel-proxy-deploy) into Claw instances, making them reachable via Cloudflare Tunnel public URLs. No relay gateway — users call the tunnel URL directly.

## Architecture

```
                         Cloudflare Tunnel
                              │
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
    ┌──────────┐       ┌──────────┐       ┌──────────┐
    │ Claw A   │       │ Claw B   │       │ Claw C   │
    │ Caddy    │       │ Caddy    │       │ Caddy    │
    │cloudflared│      │cloudflared│      │cloudflared│
    └──────────┘       └──────────┘       └──────────┘
          │                   │                   │
          ▼                   ▼                   ▼
    ┌─────────────────────────────────────────────────┐
    │             Xiaomi AI Studio API                 │
    └─────────────────────────────────────────────────┘
```

- **Account pool**: manages 8–50 accounts with 24h cooldown
- **Scheduler**: staggered deploys, handoff before 4h instance expiry
- **Proxy skill**: `cloudflared tunnel --token` + Caddy reverse proxy with client auth
- **Health monitoring**: periodic tunnel endpoint probes per account
- **Auth**: user → Caddy (PROXY_API_KEY) → MiMo (MIMO_API_KEY)

## Quick start

```bash
# Install dependencies (Python 3.12)
pip install -r requirements.txt

# Or with uv (recommended)
uv sync

# Configure
cp .env.example .env
cp config.example.json config.json
# Edit .env with your TUNNEL_TOKEN, PROXY_API_KEY
# Edit config.json with your operational params

# Dry-run scheduler
uv run --env-file .env python src/run_manager.py plan

# Run continuous operation
uv run --env-file .env python src/run_manager.py run

# Deploy a single account
uv run --env-file .env python src/deploy_one.py data/creds/user_<uid>.json deploy.v1.standard
```

## Credential files

Place account credentials as `data/creds/user_<uid>.json` (see `data/creds/` directory, gitignored).

## License

MIT

---

## User Access

mimi3 exposes OpenAI-compatible chat completions endpoints through a Cloudflare Tunnel. Grab the URL + key and call directly — no gateway needed.

### Connection info

| Item | Value |
|------|-------|
| Base URL | `https://mimo.7786.pp.ua` |
| Auth header | `Authorization: Bearer <PROXY_API_KEY>` |
| Recommended model | `mimo-v2.5` (stream + non-stream verified) |
| Endpoints | `POST /v1/chat/completions`, `GET /v1/models` |
| Compatible with | OpenAI Chat Completions API |

> Auth key is **PROXY_API_KEY** (client auth), not the upstream MIMO_API_KEY.
> MIMO_API_KEY is injected by Caddy automatically — transparent to end users.
> PROXY_API_KEY value: ask operations (stored in `.env`, never committed).

### curl

```bash
# Non-streaming
curl https://mimo.7786.pp.ua/v1/chat/completions \
  -H "Authorization: Bearer <PROXY_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"model":"mimo-v2.5","messages":[{"role":"user","content":"你好"}]}'

# Streaming (SSE)
curl -N https://mimo.7786.pp.ua/v1/chat/completions \
  -H "Authorization: Bearer <PROXY_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"model":"mimo-v2.5","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

### Python (openai SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://mimo.7786.pp.ua/v1",
    api_key="<PROXY_API_KEY>",   # PROXY_API_KEY, not MIMO_API_KEY
)

# Non-streaming
resp = client.chat.completions.create(
    model="mimo-v2.5",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)

# Streaming
stream = client.chat.completions.create(
    model="mimo-v2.5",
    messages=[{"role": "user", "content": "你好"}],
    stream=True,
)
for chunk in stream:
    delta = chunk.choices[0].delta
    if delta.content:
        print(delta.content, end="", flush=True)
```

### Available models

`GET /v1/models` returns:

| Model | Use | Status |
|-------|-----|--------|
| `mimo-v2.5` | General chat | ✅ verified |
| `mimo-v2.5-pro` | Enhanced | ⚠️ returns 400 "Param Incorrect", avoid |
| `mimo-v2.5-asr` | Speech recognition | untested |
| `mimo-v2.5-tts` | Speech synthesis | untested |
| `mimo-v2.5-tts-voiceclone` | Voice clone | untested |
| `mimo-v2.5-tts-voicedesign` | Voice design | untested |

### Response format

Standard OpenAI chat completions response. **Extra field**: `reasoning_content` (chain-of-thought, MIMO-specific) — standard SDKs ignore it harmlessly.

Non-streaming example (truncated):
```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "OK! 😊",
      "reasoning_content": "..."
    },
    "finish_reason": "stop"
  }],
  "usage": {"prompt_tokens": 249, "completion_tokens": 57, "total_tokens": 306}
}
```

### High availability

- Multiple replicas are load-balanced by Cloudflare automatically — when one replica dies, traffic shifts to the rest transparently.
- Replica count = number of Claw instances deployed and kept alive by the mimi3 account manager (target N≥8, staggered deploys for 24h uptime).
- Instance lifetime is 4h; the scheduler rolls replacements before expiry. During replacement, Cloudflare distributes across remaining replicas.

### Troubleshooting

| Symptom | Cause | Action |
|---------|-------|--------|
| 401 Unauthorized | Missing or wrong key | Check `Authorization: Bearer <PROXY_API_KEY>` |
| 502 / connection reset | All replicas down | Wait for manager to redeploy; check `run_manager.py status` |
| 400 Param Incorrect | Using `mimo-v2.5-pro` or bad request body | Switch to `mimo-v2.5`; verify OpenAI-standard request body |

MIT