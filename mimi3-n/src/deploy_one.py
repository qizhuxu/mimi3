"""
deploy_one — 单账号全流程部署 CLI 入口。

用法:
  python deploy_one.py <creds.json> [prompt_id] [deploy.v1.standard]

环境变量（均可选，未配则 L3 关闭、L1 公网探测跳过）:
  CF_API_TOKEN     Cloudflare API token（需 Tunnel:Read）。提供则启用 L3 connector 配对
  CF_ACCOUNT_ID    Cloudflare 账号 ID
  TUNNEL_TOKEN     隧道令牌 eyJ...（提供则自动解出 TUNNEL_ID，免单独配）
  TUNNEL_ID        隧道 UUID（若已知道可直接配，优先于 token 解码）
  PUBLIC_HOSTNAME  公网域名（如 mimo.7786.pp.ua）。提供则 L1 公网探测
  PROXY_API_KEY    公网探测用的 bearer（PROXY_API_KEY 鉴权）

退出码: 0=success / 1=needs_relogin(cookie 过期) / 2=其他失败
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from claw_deployer import ClawDeployer, DeployResult, build_logger, load_credentials
from prompt_store import PromptStore
from tunnel_health import TunnelHealth


def decode_tunnel_id(token: str) -> str:
    """从 eyJ... tunnel token 解出 tunnel UUID（base64({"a","t","s"}) 的 t 字段）。"""
    if not token or not token.startswith("eyJ"):
        return ""
    try:
        # eyJ... 是 base64url；补齐 padding
        pad = "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode(token + pad)
        obj = json.loads(decoded)
        return str(obj.get("t", "")).strip()
    except Exception:
        return ""


def _print_result(r: DeployResult) -> None:
    print("\n" + "=" * 60)
    print(f"DeployResult")
    print("=" * 60)
    print(f"  success          : {r.success}")
    print(f"  uid              : {r.uid}")
    print(f"  error_type       : {r.error_type}")
    print(f"  error_detail     : {r.error_detail}")
    print(f"  needs_relogin    : {r.needs_relogin}")
    print(f"  prompt_id_used   : {r.prompt_id_used}")
    print(f"  instance_status  : {r.instance_status} (remain {r.instance_remain_sec}s)")
    print(f"  connector_id     : {r.connector_id}")
    print(f"  conversation_log : {r.conversation_log}")
    print(f"  elapsed_sec      : {r.elapsed_sec:.1f}")
    print(f"  attempts         : {r.attempts}")
    if r.reply:
        print(f"  reply preview    : {r.reply[:300]}")
    print("=" * 60)


async def main() -> int:
    if len(sys.argv) < 2:
        print("用法: python deploy_one.py <creds.json> [prompt_id]", file=sys.stderr)
        return 2
    creds_path = Path(sys.argv[1])
    prompt_id = sys.argv[2] if len(sys.argv) > 2 else "deploy.v1.standard"

    logger = build_logger("deploy-one")
    base_dir = Path(__file__).resolve().parent.parent  # src/.. → 项目根

    # 加载凭据
    creds = load_credentials(creds_path)
    uid = creds.get("userId", "")
    logger.info(f"=== 单账号部署 uid={uid} prompt={prompt_id} ===")

    # PromptStore
    store = PromptStore(base_dir / "prompts" / "templates.json",
                        env_config_path=base_dir / "data" / "deploy_env.json", logger=logger)

    # 可选 L3：TunnelHealth（需 CF_API_TOKEN + CF_ACCOUNT_ID + TUNNEL_ID）
    tunnel_health = None
    cf_token = os.getenv("CF_API_TOKEN", "").strip()
    cf_account = os.getenv("CF_ACCOUNT_ID", "").strip()
    tunnel_id = os.getenv("TUNNEL_ID", "").strip()
    if not tunnel_id:
        tunnel_token = os.getenv("TUNNEL_TOKEN", "").strip()
        if tunnel_token:
            tunnel_id = decode_tunnel_id(tunnel_token)
            logger.info(f"从 TUNNEL_TOKEN 解出 TUNNEL_ID={tunnel_id}")

    if cf_token and cf_account and tunnel_id:
        tunnel_health = TunnelHealth(
            account_id=cf_account, tunnel_id=tunnel_id,
            api_token=cf_token, logger=logger,
        )
        ok = await tunnel_health.probe()
        if ok:
            logger.info("L3 启用：TunnelHealth probe 通过")
        else:
            logger.warning("L3 probe 失败，降级为 L1+L2")
            tunnel_health = None
    else:
        logger.info("L3 关闭（未配 CF_API_TOKEN/CF_ACCOUNT_ID/TUNNEL_TOKEN），仅 L1+L2 验证")

    public_hostname = os.getenv("PUBLIC_HOSTNAME", "").strip() or None
    proxy_api_key = os.getenv("PROXY_API_KEY", "").strip() or None

    deployer = ClawDeployer(
        creds, logger,
        prompt_store=store,
        out_dir=base_dir / "logs",
        tunnel_health=tunnel_health,
        public_hostname=public_hostname,
        proxy_api_key=proxy_api_key,
    )
    result = await deployer.deploy(prompt_id)
    _print_result(result)

    if result.success:
        return 0
    if result.needs_relogin:
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
