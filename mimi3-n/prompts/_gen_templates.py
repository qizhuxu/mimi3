"""生成 templates.json 的脚本。

模板文本用三引号串维护（避免手写 JSON 转义）。敏感值用 {{VAR}} 占位符，
PromptStore.get() 时从 data/deploy_env.json（或 env var）替换——这样：
1. retry 模板自包含（不再"env 同上一轮"假假设 → TUNNEL_TOKEN 空）
2. 真实 token/key 移出可提交的 templates.json，进 data/deploy_env.json（gitignored）

跑：python prompts/_gen_templates.py
"""
import json
from pathlib import Path

STANDARD = """直接部署 https://github.com/qizhuxu/cf-tunnel-proxy-deploy 这个通用 cloudflare tunnel + caddy 反代 skill。很着急快点！！！！

环境变量（已配好，直接用，不要问我，也不要输出密钥值）：
- TUNNEL_TOKEN={{TUNNEL_TOKEN}}
- PUBLIC_HOSTNAME={{PUBLIC_HOSTNAME}}
- LOCAL_PORT={{LOCAL_PORT}}
- UPSTREAM={{UPSTREAM}}
- API_KEY_ENV=MIMO_API_KEY  （MIMO_API_KEY 已在 claw env 里，Caddy 自动注入到上游，最终用户无需携带上游凭证）
- PROXY_API_KEY={{PROXY_API_KEY}}  （客户端鉴权：访问公网端点必须携带 Authorization: Bearer 这个值，否则 401）

要求：
1. git clone 到工作区 .openclaw/tmp/cf-tunnel-proxy-deploy 下（如果目录已存在，先进去 git pull 拉最新，确保用的是最新版脚本——这次新增了 PROXY_API_KEY 客户端鉴权）
2. 读 SKILL.md 和 scripts/deploy-user.sh，理解架构后立即执行
3. 非 root 环境，**必须直接 `bash scripts/deploy-user.sh` 一键执行，严禁手动 `cat > Caddyfile` 绕过脚本**。脚本里 API_KEY_LINE 的换行 bug 已在最新 commit (5ea012e) 修复，请相信脚本、直接跑。仅当脚本执行真的报错时才手动补救，且必须在汇报里贴出脚本报的具体错误。脚本会用 `cloudflared tunnel --token $TUNNEL_TOKEN` 跑（远程管理模式，不解码 token、不写本地 config.yml）。ingress 已在 Cloudflare dashboard 配好（{{PUBLIC_HOSTNAME}} → http://localhost:{{LOCAL_PORT}}）。Caddy 会要求客户端带 Authorization: Bearer $PROXY_API_KEY，无 key 或 key 错误一律 401。
4. 运行脚本时必须把所有环境变量一次性 inline 传给脚本（claw 每次执行是新 shell，不靠 export 持久化），推荐这一条命令：
   `env TUNNEL_TOKEN={{TUNNEL_TOKEN}} PUBLIC_HOSTNAME={{PUBLIC_HOSTNAME}} LOCAL_PORT={{LOCAL_PORT}} UPSTREAM={{UPSTREAM}} PROXY_API_KEY={{PROXY_API_KEY}} MIMO_API_KEY=$MIMO_API_KEY bash scripts/deploy-user.sh`
   （MIMO_API_KEY 用 $ 引用 claw 自身 env；其余值已 inline 填好。否则 Caddy 的 {env.PROXY_API_KEY}/{env.MIMO_API_KEY} 替换不出来。）
5. 部署完成后自行验证，不要让我替你验证：
   a. pgrep cloudflared && pgrep caddy  确认进程在
   b. curl -s -o /dev/null -w '%{http_code}' http://localhost:{{LOCAL_PORT}}/v1/models   （本地无 key，应 401）
   c. curl -s -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer {{PROXY_API_KEY}}' http://localhost:{{LOCAL_PORT}}/v1/models   （本地带 key，应 200）
   d. curl -s -o /dev/null -w '%{http_code}' https://{{PUBLIC_HOSTNAME}}/v1/models   （公网无 key，应 401）
   e. curl -s -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer {{PROXY_API_KEY}}' https://{{PUBLIC_HOSTNAME}}/v1/models   （公网带 key，应 200）
   f. grep -iE 'connector id|registered tunnel|connection|Invalid|error' ~/.local/log/cf-tunnel-proxy/cloudflared.log | tail -20   （拿 connector_id + 确认无 Invalid tunnel secret）
6. 全程不要问问题，遇到问题自己排查自己修，不通就自己改到通
7. **如果 cloudflared 日志出现 "Invalid tunnel secret" 或 "Unauthorized"，立即停止，不要改用其他 tunnel 或凭证，直接报告错误**
8. 只在最终完成后汇报结果，汇报格式必须包含：
   - cloudflared 的 PID
   - caddy 的 PID
   - 本地 /v1/models 无 key 的 HTTP code（应为 401）
   - 本地 /v1/models 带 key 的 HTTP code（应为 200）
   - 公网 /v1/models 无 key 的 HTTP code（应为 401）
   - 公网 /v1/models 带 key 的 HTTP code（应为 200）
   - cloudflared.log 里 connector id 那一行（原样贴出，用于副本配对）"""

RETRY = """上一轮部署未成功。请只用在已装好的 cloudflared/caddy 环境里重试，不要再装任何依赖、不要再 git clone。

环境变量（已配好，直接用，不要输出密钥值）：
- TUNNEL_TOKEN={{TUNNEL_TOKEN}}
- PUBLIC_HOSTNAME={{PUBLIC_HOSTNAME}}
- LOCAL_PORT={{LOCAL_PORT}}
- UPSTREAM={{UPSTREAM}}
- API_KEY_ENV=MIMO_API_KEY
- PROXY_API_KEY={{PROXY_API_KEY}}
- MIMO_API_KEY（已在 claw env 里，用 $ 引用）

要求：
1. 进 .openclaw/tmp/cf-tunnel-proxy-deploy 目录，git pull 拉最新
2. **必须直接 `bash scripts/deploy-user.sh` 一键执行，严禁手动 cat > Caddyfile 绕过脚本**（脚本的换行 bug 已在 commit 5ea012e 修复）
3. 运行脚本时必须把所有环境变量一次性 inline 传给脚本（claw 每次执行是新 shell，上一轮的 export 不持久）：
   `env TUNNEL_TOKEN={{TUNNEL_TOKEN}} PUBLIC_HOSTNAME={{PUBLIC_HOSTNAME}} LOCAL_PORT={{LOCAL_PORT}} UPSTREAM={{UPSTREAM}} PROXY_API_KEY={{PROXY_API_KEY}} MIMO_API_KEY=$MIMO_API_KEY bash scripts/deploy-user.sh`
4. 部署完成后自行验证：
   a. pgrep cloudflared && pgrep caddy
   b. curl -s -o /dev/null -w '%{http_code}' http://localhost:{{LOCAL_PORT}}/v1/models  （无 key 应 401）
   c. curl -s -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer {{PROXY_API_KEY}}' http://localhost:{{LOCAL_PORT}}/v1/models  （带 key 应 200）
   d. curl -s -o /dev/null -w '%{http_code}' https://{{PUBLIC_HOSTNAME}}/v1/models  （公网无 key 应 401）
   e. curl -s -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer {{PROXY_API_KEY}}' https://{{PUBLIC_HOSTNAME}}/v1/models  （公网带 key 应 200）
   f. grep -iE 'connector id|registered tunnel|connection|Invalid' ~/.local/log/cf-tunnel-proxy/cloudflared.log | tail -20
5. **cloudflared 日志出现 Invalid tunnel secret / Unauthorized 立即停止，不要换 tunnel**
6. 汇报必须含：cloudflared PID、caddy PID、本地/公网 各自 无key+带key 的 HTTP code、cloudflared.log 里 connector id 那一行（原样贴出）"""

templates = {"templates": [
    {"prompt_id": "deploy.v1.standard", "enabled": True, "text": STANDARD, "preferred_after": []},
    {"prompt_id": "deploy.v1.no_install_retry", "enabled": True, "text": RETRY,
     "preferred_after": ["deploy_refused", "verify_failed"]},
]}

out = Path(__file__).parent / "templates.json"
out.write_text(json.dumps(templates, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"wrote {out} ({out.stat().st_size} bytes, {len(templates['templates'])} templates)")
