import asyncio
import os
import re
import shutil
import time
from typing import Any

from .runtime_config import (
    ACTIVE_TUNNEL_WS_ENV,
    effective_public_base_url,
    get_config_value,
    sync_bridge_ws_env,
    update_runtime_config,
)


TRYCLOUDFLARE_RE = re.compile(r"https://[a-zA-Z0-9.-]+\.trycloudflare\.com")


class TunnelSupervisor:
    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None
        self.reader_task: asyncio.Task | None = None
        self.mode = "none"
        self.status = "disabled"
        self.public_base_url = ""
        self.ws_url = ""
        self.cloudflared_bin = "cloudflared"
        self.started_at: float | None = None
        self.restart_count = 0
        self.last_error = ""

    def _current_config(self) -> dict[str, Any]:
        mode = str(get_config_value("tunnel.mode", "none") or "none").strip()
        if mode not in {"none", "cloudflare_quick", "cloudflare_named"}:
            mode = "none"
        return {
            "mode": mode,
            "bin": str(get_config_value("tunnel.cloudflared_bin", "cloudflared") or "cloudflared").strip() or "cloudflared",
            "token": str(get_config_value("tunnel.cloudflare_tunnel_token", "") or "").strip(),
            "hostname": str(get_config_value("tunnel.cloudflare_public_hostname", "") or "").strip().strip("/"),
            "port": int(get_config_value("server.port", 8000)),
        }

    def _set_disabled_fallback(self) -> None:
        os.environ.pop(ACTIVE_TUNNEL_WS_ENV, None)
        self.public_base_url = effective_public_base_url()
        self.ws_url = sync_bridge_ws_env()

    async def start(self) -> None:
        await self.stop(clear_status=False)
        cfg = self._current_config()
        self.mode = cfg["mode"]
        self.cloudflared_bin = cfg["bin"]
        self.last_error = ""

        if self.mode == "none":
            self.status = "disabled"
            self.started_at = None
            self._set_disabled_fallback()
            return

        resolved_bin = shutil.which(self.cloudflared_bin) if not os.path.isabs(self.cloudflared_bin) else self.cloudflared_bin
        if not resolved_bin or not os.path.exists(resolved_bin):
            self.status = "missing_binary"
            self.started_at = None
            self.last_error = f"cloudflared not found: {self.cloudflared_bin}"
            self._set_disabled_fallback()
            return

        if self.mode == "cloudflare_named":
            if not cfg["token"] or not cfg["hostname"]:
                self.status = "missing_config"
                self.started_at = None
                self.last_error = "cloudflare named tunnel requires token and hostname"
                self._set_disabled_fallback()
                return
            self.public_base_url = f"https://{cfg['hostname']}"
            self.ws_url = f"wss://{cfg['hostname']}/ws"
            if "WS_TUNNEL_URL" not in os.environ:
                os.environ[ACTIVE_TUNNEL_WS_ENV] = self.ws_url
                sync_bridge_ws_env(self.ws_url)
            else:
                sync_bridge_ws_env()
            args = [resolved_bin, "tunnel", "run", "--token", cfg["token"]]
        else:
            target_url = f"http://127.0.0.1:{cfg['port']}"
            args = [resolved_bin, "tunnel", "--url", target_url]
            os.environ.pop(ACTIVE_TUNNEL_WS_ENV, None)
            self.public_base_url = ""
            self.ws_url = sync_bridge_ws_env()

        self.status = "starting"
        self.started_at = time.time()
        try:
            self.process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as exc:
            self.status = "failed"
            self.last_error = str(exc)
            self.process = None
            self._set_disabled_fallback()
            return

        self.reader_task = asyncio.create_task(self._read_output(), name="mimo-tunnel-output")
        if self.mode == "cloudflare_named":
            self.status = "running"
            return

        await self._wait_for_quick_url(timeout=8.0)

    async def _read_output(self) -> None:
        if self.process is None or self.process.stdout is None:
            return
        try:
            while True:
                raw_line = await self.process.stdout.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", "replace").strip()
                match = TRYCLOUDFLARE_RE.search(line)
                if match:
                    public_url = match.group(0).rstrip("/")
                    self.public_base_url = public_url
                    self.ws_url = f"{public_url.replace('https://', 'wss://')}/ws"
                    if "WS_TUNNEL_URL" not in os.environ:
                        os.environ[ACTIVE_TUNNEL_WS_ENV] = self.ws_url
                        sync_bridge_ws_env(self.ws_url)
                    else:
                        sync_bridge_ws_env()
                    self.status = "running"
                elif "error" in line.lower() or "failed" in line.lower():
                    self.last_error = line[:500]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = str(exc)
            if self.status == "starting":
                self.status = "failed"
        finally:
            if self.process is not None and self.process.returncode not in (None, 0):
                self.status = "failed"
                if not self.last_error:
                    self.last_error = f"cloudflared exited with code {self.process.returncode}"

    async def _wait_for_quick_url(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.status == "running" and self.ws_url:
                return
            if self.process and self.process.returncode is not None:
                if self.status == "starting":
                    self.status = "failed"
                return
            await asyncio.sleep(0.2)
        if self.status == "starting":
            self.last_error = "cloudflared started but no trycloudflare URL was detected yet"

    async def stop(self, clear_status: bool = True) -> None:
        if self.reader_task is not None:
            self.reader_task.cancel()
            await asyncio.gather(self.reader_task, return_exceptions=True)
            self.reader_task = None

        if self.process is not None and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.process = None
        os.environ.pop(ACTIVE_TUNNEL_WS_ENV, None)
        sync_bridge_ws_env()
        if clear_status:
            self.status = "stopped"
            self.public_base_url = effective_public_base_url()
            self.ws_url = sync_bridge_ws_env()

    async def restart(self) -> None:
        self.restart_count += 1
        await self.start()

    async def configure(self, updates: dict[str, Any]) -> dict[str, Any]:
        allowed_keys = {
            "tunnel.mode",
            "tunnel.cloudflared_bin",
            "tunnel.cloudflare_tunnel_token",
            "tunnel.cloudflare_public_hostname",
            "gateway.ws_tunnel_url",
            "gateway.public_base_url",
        }
        filtered = {key: value for key, value in updates.items() if key in allowed_keys}
        result = update_runtime_config(filtered)
        await self.restart()
        return result

    def snapshot(self) -> dict[str, Any]:
        cfg = self._current_config()
        running_pid = self.process.pid if self.process and self.process.returncode is None else None
        enabled = cfg["mode"] != "none"
        token_configured = bool(cfg["token"])
        hostname = cfg["hostname"]
        manual_ws_locked = "WS_TUNNEL_URL" in os.environ
        effective_ws = sync_bridge_ws_env() if manual_ws_locked else (self.ws_url or sync_bridge_ws_env())
        return {
            "mode": cfg["mode"],
            "enabled": enabled,
            "status": self.status,
            "public_base_url": self.public_base_url or effective_public_base_url(),
            "ws_url": effective_ws,
            "tunnel_ws_url": self.ws_url,
            "cloudflared_bin": cfg["bin"],
            "pid": running_pid,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "restart_count": self.restart_count,
            "token_configured": token_configured,
            "hostname": hostname,
            "manual_ws_locked": manual_ws_locked,
        }


tunnel_supervisor = TunnelSupervisor()
