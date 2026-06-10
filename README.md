# mimi3 (mimo2api)

小米 AI Studio 自动化控制网关，将 MIMO 模型进行转发并兼容。

## 功能

- OpenAI 兼容 API 中转（支持 `/v1/chat/completions`, `/v1/responses`, `/anthropic/v1/messages`）
- Web 控制面板（实时监控、日志查看）
- 生命周期监测、配置中心和 Cloudflare Tunnel 状态管理
- 网页对话 API 反代（按账号 UID 代理 AI Studio Web chat 相关接口）
- 多账号轮询负载均衡
- 流式响应支持

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 复制并配置环境变量
cp env.example .env

# 启动服务
python main.py
```

## Docker 启动

```bash
cp env.example .env
docker compose up -d --build
```

默认服务端口为 `8000`，可在 `.env` 中通过 `SERVER_PORT` 调整。

Docker Compose 会挂载以下本地目录：

- `./users` -> `/app/users`
- `./logs` -> `/app/logs`
- `./data` -> `/app/data`

容器内默认将指标数据库、指标快照、进程锁和模型映射文件放在 `/app/data`，对应宿主机 `./data` 目录：

```bash
MIMO_METRICS_DB_PATH=/app/data/gateway_metrics.db
MIMO_METRICS_SNAPSHOT_PATH=/app/data/gateway_snapshot.json
MIMO_PROCESS_LOCK_PATH=/app/data/mimo2api.lock
```

## 日志与 WS 健康判断

默认关闭 Uvicorn access log，避免 WebUI 轮询接口（如 `/api/stats`、`/api/lifecycle/status`）刷屏。需要排查 HTTP 访问明细时可临时设置：

```bash
MIMO_ACCESS_LOG=true
MIMO_LOG_LEVEL=DEBUG
```

业务日志采用 `event=... key=value` 摘要格式，注入回复和重置反馈会截断并附带摘要指纹，完整长文本不会再以 INFO 刷屏。

WS 节点连通性由网关侧维护：bridge 连接 `/ws?node=<uid>` 后写入 `node_to_ws`，并会在 WebSocket 首包和 heartbeat 中携带 `node=<uid>`，避免外网网关丢失 query 后只能看到 Unknown 节点。manager 注入成功判据是对应 UID 出现在本地或外网网关 `/api/stats` 中；如果外网网关只能报告 Unknown 节点，生命周期会显示“外网节点未识别”，manager 会停止重复注入以避免节点数膨胀。bridge 还会周期性发送 heartbeat，网关收到任意消息都会刷新 `node_last_seen_at`；生命周期页根据 `MIMO_LIFECYCLE_NODE_STALE_SECONDS` 判定 stale。

## 前置条件
一台拥有公网 ip 的机器，或者本机进行内网穿透。此为必备配置选项
```bash
WS_TUNNEL_URL=ws://your-domain.com:8000/ws
```

也可以在 Web 控制台的“网关出口”中配置内置 Cloudflare Tunnel。`WS_TUNNEL_URL` 如果通过环境变量显式设置，会作为手动地址锁定；运行时配置会写入 `data/runtime_config.json`，不会修改 `.env`。

Web 控制台包含：

- 总览：节点、请求、延迟、最近错误
- 生命周期：账号云端状态、bridge 在线状态、冷却与异常判断
- 账号管理：导入和删除 `users/` 运行目录中的账号凭证
- 模型映射：维护 `model_mapping.json`
- 网关出口：Cloudflare 临时/固定隧道状态和配置
- 系统配置：WebUI、API 鉴权、生命周期策略等运行时配置

## 网页对话反代

按账号 UID 暴露 AI Studio Web chat 相关接口，路径格式：

```text
/api/web-chat/<uid>/open-apis/bot/chat
/api/web-chat/<uid>/open-apis/chat/conversation/list
/api/web-chat/<uid>/open-apis/chat/dialog/list
```

`/api/web-chat/*` 受 WebUI 登录保护。WebSocket 代理为：

```text
/api/web-chat/<uid>/ws/proxy
```

HTTP 代理会自动补齐 `xiaomichatbot_ph` 查询参数，并使用本机 `users/user_<uid>.json` 中的凭证访问上游。

## 免责声明

1. **本项目仅供学习交流使用，禁止一切商业/滥用行为。**
2. 本项目为个人独立开发的开源项目，与小米公司及其关联方**无任何隶属、授权或合作关系**。
3. MIMO、Xiaomi AI Studio 等名称及商标归小米公司所有，本项目不主张任何权利。
4. 本项目不提供任何小米账号、密钥或付费服务的破解，仅作为技术研究用途。
5. 使用者应遵守所在地法律法规及小米服务条款，因使用本项目产生的一切后果由使用者自行承担。
6. 本项目代码随缘更新，作者不提供任何保证或技术支持。
7. **建议优先使用小米官方 API**，本项目仅为技术研究备选方案。
8. 如有任何权益问题，请联系删除。

## 致谢
[linux.do](https://linux.do)
