import asyncio, websockets, httpx, json, os
import time
from urllib.parse import parse_qs, urlsplit

KEY = os.getenv("MIMO_API_KEY")
URL = os.getenv("MIMO_API_ENDPOINT")
BASE = URL.split("/v1/")[0] if "/v1/" in URL else URL
WS_URL = "__WS_URL__"
BRIDGE_HEARTBEAT_SECONDS = int(os.getenv("MIMO_BRIDGE_HEARTBEAT_SECONDS", "30") or 30)
NODE_ID = (parse_qs(urlsplit(WS_URL).query).get("node") or parse_qs(urlsplit(WS_URL).query).get("node_id") or [""])[0]

async def safe_send(ws, lock, data):
    async with lock:
        await ws.send(json.dumps(data))

async def handle_request(ws, req, client, lock):
    req_id = req.get("req_id") 
    try:
        async with client.stream(
            method=req.get("method", "GET"), 
            url=f"{BASE}/anthropic/v1/messages" if "/anthropic/" in req.get("path", "") else URL, 
            headers={"api-key": KEY, "Content-Type": "application/json"}, 
            content=req.get("body", "")
        ) as r:
            await safe_send(ws, lock, {
                "req_id": req_id, "type": "start", 
                "status": r.status_code, "headers": dict(r.headers)
            })
            async for chunk in r.aiter_text():
                if chunk:
                    await safe_send(ws, lock, {
                        "req_id": req_id, "type": "chunk", "body": chunk
                    })
            await safe_send(ws, lock, {"req_id": req_id, "type": "finish"})
            
    except Exception as e:
        await safe_send(ws, lock, {"req_id": req_id, "type": "error", "body": str(e)})

async def heartbeat_loop(ws, lock):
    while True:
        await asyncio.sleep(max(5, BRIDGE_HEARTBEAT_SECONDS))
        await safe_send(ws, lock, {"type": "heartbeat", "node": NODE_ID, "ts": int(time.time())})

async def main():
    async with httpx.AsyncClient(timeout=None) as client:
        while True:
            try:
                async with websockets.connect(WS_URL, max_size=10**8) as ws:
                    send_lock = asyncio.Lock()
                    await safe_send(ws, send_lock, {"type": "hello", "node": NODE_ID, "ts": int(time.time())})
                    heartbeat_task = asyncio.create_task(heartbeat_loop(ws, send_lock))
                    try:
                        async for msg in ws:
                            asyncio.create_task(handle_request(ws, json.loads(msg), client, send_lock))
                    finally:
                        heartbeat_task.cancel()
                        await asyncio.gather(heartbeat_task, return_exceptions=True)
            except Exception:
                await asyncio.sleep(3)

if __name__ == "__main__":
    asyncio.run(main())
