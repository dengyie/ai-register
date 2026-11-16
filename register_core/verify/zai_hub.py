"""z.ai 账号 hub（zcode2api）入池核验 verifier + hub HTTP 客户端。

层方向：本模块属 register_core（verify 层），产品侧（providers/zai）经
``hub_http_json``/``pool_verify_account`` 反向复用——产品→core 单向。

pool_verify_account 门禁语义（2026-11-16 定版，**首日核验**）：
在池 + active/selectable/enabled/strikes=0 + GLM-5.3 与 GLM-5.3-Flash 双额度满。
新号 quota 快照首轮 billing 前为空（None/None）→ 轮询等待；存量号额度被真实
流量耗尽后永不满足——勿用于存量巡检。

四步 OAuth 直投（login/start → approve → GET redirect_url → poll ready）的
join 侧在 providers/zai/protocol/hub.py（注册域逻辑），共享同一 hub HTTP 客户端。
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any

from register_core.contracts import RegisterResult, VerifyResult

UA = {"User-Agent": "Mozilla/5.0"}

POOL_VERIFY_TIMEOUT_S = 180
POOL_VERIFY_INTERVAL_S = 15


def hub_http_json(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    data: dict[str, Any] | None = None,
    *,
    form: bool = False,
    timeout: int = 30,
) -> tuple[int, Any]:
    """hub 管理 API 统一 HTTP 客户端（直连，绝不经系统代理）。

    返回 (status, parsed-json-or-raw)；非 JSON 响应原样返回字符串供上层归因。
    """
    h = dict(UA)
    h.update(headers or {})
    body = None
    if data is not None:
        body = (urllib.parse.urlencode(data).encode() if form
                else json.dumps(data).encode())
        h.setdefault("Content-Type",
                     "application/x-www-form-urlencoded" if form else "application/json")
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            req, timeout=timeout) as r:
        raw = r.read().decode()
    try:
        return r.status, json.loads(raw)
    except Exception:
        return r.status, raw


def pool_verify_account(
    label: str,
    *,
    hub_url: str,
    hub_key: str,
    max_wait_s: float = POOL_VERIFY_TIMEOUT_S,
    interval_s: float = POOL_VERIFY_INTERVAL_S,
    log=None,
) -> tuple[bool, str]:
    """入池核验门禁（真值表：在池×gate×双额度）。

    非 200（鉴权失败/网关抖动）只告警一次后继续轮询，避免 401 时白等无提示。
    """
    def say(msg: str) -> None:
        if log:
            log(msg)

    deadline = time.time() + max_wait_s
    warned_non200 = False
    while True:
        try:
            st, d = hub_http_json(
                "GET", f"{hub_url}/admin/api/accounts",
                headers={"Authorization": f"Bearer {hub_key}"})
        except Exception as exc:
            say(f"accounts err: {type(exc).__name__}")
            st, d = 0, {}
        if st != 200:
            if not warned_non200:
                warned_non200 = True
                say(f"accounts http {st}（将重试至 {max_wait_s:.0f}s；"
                    f"持续非 200 请检查 ZAI_HUB_KEY）")
        accts = []
        if st == 200:
            accts = (d if isinstance(d, list)
                     else d.get("accounts") or d.get("data") or [])
        a = next((x for x in accts if (x.get("name") or "") == label), None)
        if a is not None:
            q = a.get("quota") or {}
            g = q.get("GLM-5.3") or {}
            f = q.get("GLM-5.3-Flash") or {}
            gate = (a.get("status") == "active" and a.get("selectable")
                    and a.get("enabled") and not a.get("risk_strikes"))
            quota_ok = (g.get("remaining") or 0) > 0 and (f.get("remaining") or 0) > 0
            if gate and quota_ok:
                say(f"✓ {label}: active/selectable/strikes=0, "
                    f"GLM-5.3 {g['remaining']}/{g['total']}, Flash {f['remaining']}/{f['total']}")
                return True, "pool_verified"
            say(f"核验未满 gate={gate} quota=({g.get('remaining')}/{f.get('remaining')})（等首轮 billing）")
        else:
            say(f"池内未找到 {label}（重查）")
        if time.time() >= deadline:
            return False, f"{max_wait_s:.0f}s 内未通过池核验"
        time.sleep(interval_s)


class ZaiHubVerifier:
    name = "zai_hub"

    def __init__(
        self,
        *,
        hub_url: str | None = None,
        hub_key: str | None = None,
        max_wait_s: float | None = None,
        **_: Any,
    ) -> None:
        self.hub_url = str(
            hub_url or os.environ.get("ZAI_HUB_URL")
            or "https://zcode.mangoqwq.com").rstrip("/")
        self.hub_key = str(
            hub_key or os.environ.get("ZAI_HUB_KEY") or "").strip()
        self.max_wait_s = float(
            max_wait_s or os.environ.get("ZAI_POOL_VERIFY_WAIT") or 180)

    def verify(self, result: RegisterResult) -> VerifyResult:
        if not result.ok:
            return VerifyResult(ok=False, provider="zai", capability="hub_pool",
                                detail="registration failed; nothing to verify")
        if not self.hub_key:
            return VerifyResult(ok=False, provider="zai", capability="hub_pool",
                                detail="ZAI_HUB_KEY not set (operator must join+verify)")
        label = str((result.artifacts or {}).get("hub_label") or "")
        if not label:
            return VerifyResult(ok=False, provider="zai", capability="hub_pool",
                                detail="artifacts.hub_label missing")

        ok, detail = pool_verify_account(
            label, hub_url=self.hub_url, hub_key=self.hub_key,
            max_wait_s=self.max_wait_s)
        return VerifyResult(ok=ok, provider="zai", capability="hub_pool",
                            detail=detail, meta={"hub_label": label})
