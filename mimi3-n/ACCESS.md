# mimi3-n 接入指南

mimi3-n 通过 Cloudflare Tunnel 暴露 OpenAI 兼容的 chat completions 端点。
用户拿 URL + key 直接调用，无需自建 gateway。

## 接入信息

| 项 | 值 |
|---|---|
| Base URL | `https://mimo.7786.pp.ua` |
| 鉴权头 | `Authorization: Bearer <PROXY_API_KEY>` |
| 推荐模型 | `mimo-v2.5`（流式 + 非流式均已验证） |
| 端点 | `POST /v1/chat/completions`、`GET /v1/models` |
| 兼容 | OpenAI Chat Completions API |

> 鉴权 key 是 **PROXY_API_KEY**（客户端鉴权），不是上游 MIMO_API_KEY。
> MIMO_API_KEY 由 Caddy 自动注入上游，用户侧无感。
> PROXY_API_KEY 值向运营索取（运营侧在 `data/deploy_env.json`，不入仓库）。

## curl

```bash
# 非流式
curl https://mimo.7786.pp.ua/v1/chat/completions \
  -H "Authorization: Bearer <PROXY_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"model":"mimo-v2.5","messages":[{"role":"user","content":"你好"}]}'

# 流式（SSE）
curl -N https://mimo.7786.pp.ua/v1/chat/completions \
  -H "Authorization: Bearer <PROXY_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"model":"mimo-v2.5","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

## Python (openai SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://mimo.7786.pp.ua/v1",
    api_key="<PROXY_API_KEY>",   # PROXY_API_KEY
)

# 非流式
resp = client.chat.completions.create(
    model="mimo-v2.5",
    messages=[{"role": "user", "content": "你好"}],
)
print(resp.choices[0].message.content)

# 流式
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

## 可用模型

`GET /v1/models` 返回：

| 模型 | 用途 | 状态 |
|---|---|---|
| `mimo-v2.5` | 通用对话 | ✅ 已验证 |
| `mimo-v2.5-pro` | 增强 | ⚠️ 实测返 400 "Param Incorrect"，暂避用 |
| `mimo-v2.5-asr` | 语音识别 | 未测 |
| `mimo-v2.5-tts` | 语音合成 | 未测 |
| `mimo-v2.5-tts-voiceclone` | 声音克隆 | 未测 |
| `mimo-v2.5-tts-voicedesign` | 声音设计 | 未测 |

## 响应格式

标准 OpenAI chat completions 响应。**额外字段**：`reasoning_content`（思维链，mimo 特有），
OpenAI 标准 SDK 自动忽略，不影响兼容。

非流式示例（截取）：
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

## 高可用

- 多 replica 由 Cloudflare 自动负载均衡，单 replica 挂了自动切其他，用户无感。
- replica 数 = mimi3-n 账号管理器部署并保持存活的 claw 实例数（目标 N≥8，错峰部署保 24h 不间断）。
- 实例寿命 4h，到期由管理器滚动补位；补位期间若 replica 数下降，CF 在剩余 replica 间分流。

## 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 401 Unauthorized | 未带 key 或 key 错 | 检查 `Authorization: Bearer <PROXY_API_KEY>` |
| 502 / 连接重置 | 所有 replica 都挂了 | 等 mimi3-n 管理器补部署；查 `run_manager.py status` |
| 400 Param Incorrect | 用了 mimo-v2.5-pro 或请求体格式错 | 换 `mimo-v2.5`；核对 OpenAI 标准请求体 |
