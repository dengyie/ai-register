"""HTTP session helpers for the Outlook MSA protocol flow.

Mirrors providers/chatgpt/protocol/session.py (curl_cffi preferred for the TLS
fingerprint, requests fallback) with Windows/Chrome identity from constants.
"""

from __future__ import annotations

import time
from typing import Any

from .constants import DEFAULT_TIMEOUT, USER_AGENT


def create_session(proxy: str = "") -> Any:
    """curl_cffi session pinned to a chrome impersonate; requests fallback.

    The UA in constants is Chrome/Windows; chrome* impersonate profiles are the
    matching TLS shape. verify=False matches the chatgpt session behaviour for
    MITM'd proxy chains.
    """
    proxy = (proxy or "").strip()
    try:
        from curl_cffi import requests as curl_requests

        kwargs: dict[str, Any] = {"impersonate": "chrome131", "verify": False}
        if proxy:
            kwargs["proxy"] = proxy
        return curl_requests.Session(**kwargs)
    except Exception:
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        session = requests.Session()
        retry = Retry(
            total=2,
            connect=2,
            read=2,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        session.verify = False
        session.headers["user-agent"] = USER_AGENT
        if proxy:
            session.proxies.update({"http": proxy, "https": proxy})
        return session


def request_with_retry(
    session: Any,
    method: str,
    url: str,
    *,
    retry_attempts: int = 3,
    timeout: float = DEFAULT_TIMEOUT,
    **kwargs: Any,
) -> tuple[Any | None, str]:
    """Send once per attempt; return (response|None, last_error)."""
    last_error = ""
    for _ in range(max(1, retry_attempts)):
        try:
            return session.request(method.upper(), url, timeout=timeout, **kwargs), ""
        except Exception as exc:
            last_error = str(exc)
            time.sleep(1)
    return None, last_error


def response_json(resp: Any) -> dict:
    if resp is None:
        return {}
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def response_text(resp: Any) -> str:
    if resp is None:
        return ""
    try:
        return str(resp.text or "")
    except Exception:
        return ""
