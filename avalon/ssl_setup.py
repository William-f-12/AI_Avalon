"""让 Python 信任公司安全网关(MITM SWG，如 SealSuite)的根证书。

pyenv / conda 安装的 Python 只信任 certifi 自带的 CA 包，不含公司网关用于
TLS 拦截的自签根证书。于是访问 https://api.deepseek.com 会握手失败
（SSL: CERTIFICATE_VERIFY_FAILED），openai SDK 抛 APIConnectionError，
最终导致 agent 每个决策都失败、发言全变「（沉默）」。

curl 能成功是因为它走 macOS 系统钥匙串（里面装了公司根证书）。这里把
certifi + 系统/登录钥匙串里的 CA 合并成一个证书包，通过 SSL_CERT_FILE 指给
所有 https 请求，从而和 curl 信任同一套 CA。仅在 macOS 生效；其它平台回退到
certifi 默认行为。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import threading

import certifi

# 系统级钥匙串（公司根证书常部署在 System.keychain）
_SYSTEM_KEYCHAINS = [
    "/Library/Keychains/System.keychain",
    "/System/Library/Keychains/SystemRootCertificates.keychain",
]

# 跨局并行时各局有各自的 LLMClient（各自的锁），首次调用会并发进入本函数；
# 用模块级锁串行化构建，避免并发写同一份 CA 缓存文件产生半截 PEM。
_TRUST_LOCK = threading.Lock()


def _login_keychain() -> list[str]:
    path = os.path.expanduser("~/Library/Keychains/login.keychain-db")
    return [path] if os.path.exists(path) else []


def _export_pem(keychain: str) -> bytes:
    """导出某个钥匙串里的全部证书（PEM）。失败返回空。"""
    try:
        out = subprocess.run(
            ["security", "find-certificate", "-a", "-p", keychain],
            capture_output=True, timeout=15,
        )
        return out.stdout or b""
    except Exception:
        return b""


def ensure_system_trust() -> None:
    """把 macOS 钥匙串的 CA 并入 SSL 信任链。设置 SSL_CERT_FILE 后即对 httpx/openai 生效。

    幂等：通过 AVALON_SSL_READY 标记避免重复构建。非 macOS 平台仅回退到 certifi。
    """
    if os.environ.get("AVALON_SSL_READY"):
        return

    with _TRUST_LOCK:
        if os.environ.get("AVALON_SSL_READY"):  # 双检：可能已被另一线程构建
            return

        if sys.platform != "darwin":
            os.environ.setdefault("SSL_CERT_FILE", certifi.where())
            os.environ["AVALON_SSL_READY"] = "1"
            return

        parts: list[bytes] = []
        with open(certifi.where(), "rb") as f:
            parts.append(f.read())
        for kc in _SYSTEM_KEYCHAINS + _login_keychain():
            pem = _export_pem(kc)
            if pem:
                parts.append(pem)

        bundle = b"\n".join(parts)
        # 用内容哈希命名并缓存，证书变化时自动换新文件
        digest = hashlib.md5(bundle).hexdigest()[:10]
        cache = os.path.join(tempfile.gettempdir(), f"avalon-cacert-{digest}.pem")
        if not os.path.exists(cache):
            # 原子写：先写临时文件再 rename，避免并发/中断产生半截 PEM
            fd, tmp = tempfile.mkstemp(dir=tempfile.gettempdir(),
                                       prefix="avalon-cacert-", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(bundle)
                os.replace(tmp, cache)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

        os.environ["SSL_CERT_FILE"] = cache
        os.environ["SSL_CERT_DIR"] = os.path.dirname(cache)
        os.environ["AVALON_SSL_READY"] = "1"  # 最后置位：确保其它线程不会读到半成品环境
