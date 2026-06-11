# HuggingFace Space 部署说明

本分支面向 HuggingFace Space Docker 部署，默认镜像标签为 `mimi3:hf_latest`，默认监听端口为 `7860`。

## 默认环境

```bash
MIMO_DEPLOY_TARGET=hf_space
SERVER_HOST=0.0.0.0
SERVER_PORT=7860
MIMO_TUNNEL_MODE=none
```

`MIMO_DEPLOY_TARGET=hf_space` 会启用 HF Space 保护逻辑：

- `server.port` 未显式配置时默认使用 `7860`。
- 内置 Cloudflare Tunnel 强制不可用，即使环境变量或运行时配置里写了 `cloudflare_quick` / `cloudflare_named`。
- WebUI 隐藏“网关出口”入口，并隐藏 Cloudflare Tunnel 相关运行时配置项。
- `/api/tunnel/status` 仍可返回状态，用于诊断当前部署目标和有效 WS 地址。

## Bridge 回连地址

HF Space 本身会提供公网 HTTPS 地址。部署后请把 Claw bridge 的 WebSocket 地址配置为：

```text
wss://<your-space-host>/ws
```

如果需要手动锁定地址，设置：

```bash
WS_TUNNEL_URL=wss://<your-space-host>/ws
```

`WS_TUNNEL_URL` 一旦作为环境变量显式设置，会继续作为手动锁定值使用，WebUI 不会覆盖 `.env`。

## 为什么禁用 Cloudflare Tunnel

HuggingFace Space 环境已经提供公网入口，不需要在容器内再启动 Cloudflare Tunnel。继续启动隧道可能触发平台风控或账号封禁风险，因此本分支在 `hf_space` 模式下强制禁用内置 Tunnel supervisor。

## 本地验证

```bash
docker build -t mimi3:hf_latest .
docker run --rm -p 7860:7860 --env-file .env mimi3:hf_latest
```

健康检查使用容器内 `SERVER_PORT`，默认访问：

```text
http://127.0.0.1:7860/api/auth/session
```
