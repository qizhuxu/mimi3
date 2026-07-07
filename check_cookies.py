"""
快速检测所有账号 cookie 是否失效。
对每个 creds 文件，调用 Claw 状态 API，标记 HTTP 401 的账号。
"""
import asyncio
import json
import os
import sys
from pathlib import Path

# 让 Python 找到 src/ 模块
SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from claw_deployer import credentials_to_client_params
from health_monitor import probe_status

CREDS_DIR = Path("data/creds")
STATE_DIR = Path("data/state")


async def check_account(uid, creds):
    """检查单个账号 cookie 有效性。返回 (uid, is_expired, status, note)。"""
    ph, cookies, _ = credentials_to_client_params(creds)
    cloud_st, remain, http = await probe_status(cookies)

    is_expired = (http == 401)

    # 读取 state
    state_path = STATE_DIR / f"user_{uid}.state.json"
    state_info = ""
    if state_path.exists():
        try:
            with open(state_path, encoding="utf-8") as f:
                d = json.load(f)
            state_info = f"ds={d.get('deploy_state','?')} lr={d.get('last_result','?')}"
        except:
            state_info = "state_corrupt"

    note = f"status={cloud_st} remain={remain}s http={http} {state_info}"
    return uid, is_expired, note


async def main():
    creds_dir = CREDS_DIR
    if not creds_dir.exists():
        print(f"错误: {creds_dir} 不存在")
        return

    files = sorted(creds_dir.iterdir())
    print(f"共 {len(files)} 个账号，正在检测 cookie 有效性...\n")

    expired = []
    valid = []
    errors = []

    for f in files:
        if not f.name.endswith(".json"):
            continue
        uid = f.name.replace(".json", "").replace("user_", "")
        try:
            with open(f, encoding="utf-8") as fh:
                creds = json.load(fh)
        except Exception as e:
            print(f"  [?] {uid}  读取失败: {e}")
            errors.append(uid)
            continue

        result = await check_account(uid, creds)
        uid, is_expired, note = result

        if is_expired:
            print(f"  [401] {uid}  cookie已失效  {note}")
            expired.append(uid)
        else:
            print(f"  [OK]  {uid}  {note}")
            valid.append(uid)

    await asyncio.sleep(0)  # flush pending

    print(f"\n=== 汇总 ===")
    print(f"有效: {len(valid)}")
    print(f"失效(401): {len(expired)}")
    print(f"读取失败: {len(errors)}")

    if expired:
        print(f"\n以下账号 cookie 已失效，建议移除:")
        for uid in expired:
            print(f"  user_{uid}.json  +  state/user_{uid}.state.json")


if __name__ == "__main__":
    asyncio.run(main())