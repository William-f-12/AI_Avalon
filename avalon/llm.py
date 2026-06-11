"""Provider 无关的 LLM 客户端（默认 DeepSeek，OpenAI 兼容）。

通过 config 指定 model / base_url / api_key_env，即可切到 OpenAI、Claude 兼容端点等，
而无需改动 agent 代码。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Optional

# MODEL_NAME = "deepseek-v4-pro"
MODEL_NAME = "deepseek-v4-flash"


def _is_local_url(url: str) -> bool:
    """本地推理端点（Ollama / LM Studio 等），不校验 API 密钥。"""
    return any(h in url for h in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]"))


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(
        self,
        model: str = MODEL_NAME,
        base_url: str = "https://api.deepseek.com",
        api_key_env: str = "DEEPSEEK_API_KEY",
        temperature: float = 0.8,
        max_retries: int = 3,
        timeout: float = 60.0,
    ):
        self.model = model
        self.base_url = base_url
        self.api_key_env = api_key_env
        self.temperature = temperature
        self.max_retries = max_retries
        self.timeout = timeout
        self._client = None  # 懒加载
        self._lock = threading.Lock()  # 防并发首次初始化竞态（投票/任务牌并发时）

    def _ensure_client(self):
        if self._client is not None:
            return
        with self._lock:
            if self._client is not None:  # 双检：可能已被另一线程初始化
                return
            # 确保信任公司安全网关(MITM)的根证书，否则 TLS 握手会失败
            from .ssl_setup import ensure_system_trust
            ensure_system_trust()
            try:
                from openai import OpenAI
            except ImportError as e:  # pragma: no cover
                raise LLMError("需要安装 openai 库：pip install openai") from e
            api_key = os.environ.get(self.api_key_env)
            if not api_key:
                # 本地端点（Ollama 等）不校验密钥，OpenAI SDK 仅要求非空，给个占位即可；
                # 远端仍强制要真 key，避免漏配静默打到错误端点。
                if _is_local_url(self.base_url):
                    api_key = "local"
                else:
                    raise LLMError(
                        f"未找到 API 密钥，请设置环境变量 {self.api_key_env}（可写在 .env 中）。"
                    )
            self._client = OpenAI(
                api_key=api_key, base_url=self.base_url, timeout=self.timeout)

    def complete(
        self,
        system: str,
        user: str,
        json_mode: bool = True,
    ) -> str:
        """单轮补全。json_mode 时要求模型输出 JSON 对象。返回文本（通常是 JSON 字符串）。"""
        self._ensure_client()
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries):
            try:
                resp = self._client.chat.completions.create(**kwargs)
                return resp.choices[0].message.content or ""
            except Exception as e:  # noqa: BLE001 - 统一退避重试
                last_err = e
                # 指数退避：1s, 2s, 4s ...
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)
        raise LLMError(f"LLM 调用失败（重试 {self.max_retries} 次）：{last_err}")
