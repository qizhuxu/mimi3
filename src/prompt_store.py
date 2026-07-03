"""
prompt_store — 注入 prompt 模板存储，从文件加载（不硬编码）。

容器化部署把 prompts/ 作为 volume 挂载，改话术不动镜像。
模板汇报格式要求附 cloudflared.log 的 connector_id 行（L2 数据来源）——
加载时校验，缺则警告（不阻塞，调用方决定是否用）。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional


# 占位符 {{VAR}}：PromptStore.get/next_after 时用 config.json（或 env var）替换。
# 双花括号避免与 prompt 里的 Caddy {env.X} 单花括号引用冲突。
_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


@dataclass(frozen=True)
class PromptTemplate:
    prompt_id: str
    text: str
    # 失败类名：deploy_refused / verify_failed 等。deployer 在这类失败后
    # 从 store 取 preferred_after 匹配的下一个模板限量重试。
    preferred_after: tuple[str, ...] = field(default_factory=tuple)
    enabled: bool = True


class PromptStore:
    """从 JSON 文件加载 prompt 模板。支持热加载（容器挂载更新后）。"""

    def __init__(self, path: Path, env_config_path: Optional[Path] = None,
                 logger: Optional[logging.Logger] = None):
        self.path = Path(path)
        self.env_config_path = Path(env_config_path) if env_config_path else None
        self._logger = logger or logging.getLogger("prompt-store")
        self._templates: dict[str, PromptTemplate] = {}
        self._env_values: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        """重新加载模板 + env 配置。容器挂载更新后可调，不重启进程。"""
        self._load_env()
        with open(self.path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        items = raw.get("templates", []) if isinstance(raw, dict) else []
        self._templates = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("prompt_id", "")).strip()
            if not pid:
                continue
            text = str(item.get("text", ""))
            pa = tuple(str(x) for x in item.get("preferred_after", []))
            enabled = bool(item.get("enabled", True))
            tpl = PromptTemplate(prompt_id=pid, text=text, preferred_after=pa, enabled=enabled)
            self._templates[pid] = tpl
            # 校验：模板应要求汇报 connector_id（L2 数据来源）
            if "connector id" not in text.lower() and "connector_id" not in text.lower():
                self._logger.warning(
                    "prompt 模板 %s 未要求汇报 connector_id，L2 验证可能拿不到配对数据", pid
                )
        self._logger.info("prompt_store 已加载 %d 个模板: %s", len(self._templates), list(self._templates))

    def _load_env(self) -> None:
        """加载 env 替换值：config.json["prompt_store"]["substitution_values"]
        或旧 data/deploy_env.json（迁移兼容）。os.getenv 优先于文件值。"""
        self._env_values = {}
        # 1. config.json（在项目根目录）
        cfg = self.path.parent.parent.parent / "config.json"
        if cfg.exists():
            try:
                with open(cfg, encoding="utf-8") as f:
                    raw = json.load(f)
                subs = raw.get("prompt_store", {}).get("substitution_values", {})
                if isinstance(subs, dict):
                    for k, v in subs.items():
                        if not k.startswith("_"):
                            self._env_values[str(k)] = "" if v is None else str(v)
            except Exception as e:
                self._logger.warning("读取 %s 替换值失败: %s", cfg, e)
        # 2. 旧路：deploy_env.json（迁移兼容）
        if not self._env_values and self.env_config_path and self.env_config_path.exists():
            try:
                with open(self.env_config_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        self._env_values[str(k)] = "" if v is None else str(v)
            except Exception as e:
                self._logger.warning("读取 %s 失败: %s", self.env_config_path, e)

    def _substitute(self, text: str) -> str:
        """把 {{VAR}} 替换为 env 值（os.getenv 优先于配置文件）。
        缺值则 WARN 并保留字面占位符——让问题暴露，而非静默发送空值（那正是
        retry 模板"env 同上一轮"假假设导致 TUNNEL_TOKEN 空的 bug 根因）。"""
        def _repl(m):
            var = m.group(1)
            val = os.getenv(var, "").strip() or self._env_values.get(var, "")
            if not val:
                self._logger.warning(
                    "prompt 占位符 {{%s}} 无值（.env 和 config.json 都没配），将带字面占位符发送",
                    var)
                return m.group(0)
            return val
        return _PLACEHOLDER_RE.sub(_repl, text)

    def _resolved(self, tpl: PromptTemplate) -> PromptTemplate:
        """返回 text 已替换占位符的副本（PromptTemplate 是 frozen，用 replace）。"""
        return replace(tpl, text=self._substitute(tpl.text))

    def get(self, prompt_id: str) -> PromptTemplate:
        """取指定模板（text 已替换 env 占位符）。未启用或不存在则抛 KeyError。"""
        tpl = self._templates.get(prompt_id)
        if tpl is None:
            raise KeyError(f"prompt 模板不存在: {prompt_id}")
        if not tpl.enabled:
            raise KeyError(f"prompt 模板已禁用: {prompt_id}")
        return self._resolved(tpl)

    def next_after(self, failure_class: str, used: set[str]) -> Optional[PromptTemplate]:
        """失败后选下一个模板：preferred_after 含 failure_class、未用过、已启用。
        返回的 text 已替换 env 占位符——retry 模板自包含，不靠"上一轮 env"（那会空）。

        used 是已经用过的 prompt_id 集合（避免无限轮换）。
        """
        for pid, tpl in self._templates.items():
            if not tpl.enabled or pid in used:
                continue
            if failure_class in tpl.preferred_after:
                return self._resolved(tpl)
        return None

    def all_ids(self) -> list[str]:
        return [pid for pid, tpl in self._templates.items() if tpl.enabled]


if __name__ == "__main__":
    # 冒烟测
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    store = PromptStore(Path(__file__).parent.parent / "data" / "prompts" / "templates.json")
    print("ids:", store.all_ids())
    t = store.get("deploy.v1.standard")
    print(f"standard: text_len={len(t.text)} preferred_after={t.preferred_after}")
    print("contains 'connector id':", "connector id" in t.text.lower())
    print("has unresolved {{}}:", "{{" in t.text)            # 应 False
    print("contains TUNNEL_TOKEN value (ey...):", "eyJ" in t.text)  # 应 True（已替换）
    nxt = store.next_after("deploy_refused", {"deploy.v1.standard"})
    print("next_after deploy_refused:", nxt.prompt_id if nxt else None)
    if nxt:
        print("retry has unresolved {{}}:", "{{" in nxt.text)        # 应 False
        print("retry contains PROXY_API_KEY value (sk-):", "sk-qwedc" in nxt.text)  # 应 True
