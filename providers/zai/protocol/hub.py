"""zcode-hub（zcode2api）OAuth 四步直投（注册域逻辑）。

四步（2026-11-13 定版）：login/start → form 编码 approve → **立刻 GET
redirect_url 落码**（缺这步官方 poll 恒 pending、code 数分钟过期）→ poll ready。
hub join 属**操作员显式命令**（join-hub.sh），不混入自动注册。

池核验门禁在 register_core/verify/zai_hub.py（core 层，共享 hub HTTP 客户端）。
"""

from __future__ import annotations

import json
import time
import urllib.parse
from typing import Any

from register_core.verify.zai_hub import hub_http_json

CLIENT_ID = "client_P8X5CMWmlaRO9gyO-KSqtg"
REDIRECT_URI = "https://zcode.z.ai/api/v1/oauth/cli/callback/zai"


def hub_join(
    token: str, label: str, *, hub_url: str, hub_key: str, log=None
) -> tuple[bool, str]:
    """四步直投；返回 (ok, message)。任何一步重试耗尽即失败。

    flow_id 5 分钟过期；callback GET 必须在 approve 后立刻执行（code 数分钟过期）。
    """
    def say(msg: str) -> None:
        if log:
            log(msg)

    start: dict[str, Any] | None = None
    for t in range(4):
        try:
            st, start = hub_http_json(
                "POST", f"{hub_url}/admin/api/login/start",
                headers={"Authorization": f"Bearer {hub_key}"},
                data={"label": label})
            if st == 200 and isinstance(start, dict) and start.get("flow_id"):
                break
            say(f"start try{t}: http {st}")
        except Exception as exc:
            say(f"start try{t}: {type(exc).__name__}")
        time.sleep(8)
    if not (isinstance(start, dict) and start.get("flow_id")):
        return False, "login/start 无 flow_id"
    flow_id = start["flow_id"]
    state = urllib.parse.parse_qs(
        urllib.parse.urlparse(start["authorize_url"]).query)["state"][0]
    say(f"flow={flow_id}")

    redirect: str | None = None
    for t in range(3):
        try:
            st, resp = hub_http_json(
                "POST", "https://chat.z.ai/api/oauth/authorize",
                headers={"Authorization": f"Bearer {token}"},
                data={"client_id": CLIENT_ID, "redirect_uri": REDIRECT_URI,
                      "state": state, "response_type": "code", "action": "approve"},
                form=True)
            if st == 200 and isinstance(resp, dict) and resp.get("redirect_url"):
                redirect = resp["redirect_url"]
                break
            say(f"approve try{t}: http {st} {str(resp)[:120]}")
        except Exception as exc:
            say(f"approve try{t}: {type(exc).__name__}")
        time.sleep(8)
    if not redirect:
        return False, "approve 未取得 redirect_url"

    try:
        st, body = hub_http_json("GET", redirect, timeout=30)
        say(f"callback GET {st}（落码）")
    except Exception as exc:
        say(f"callback GET err: {type(exc).__name__}")

    for i in range(12):
        time.sleep(4)
        try:
            st, poll = hub_http_json(
                "GET", f"{hub_url}/admin/api/login/poll/{flow_id}",
                headers={"Authorization": f"Bearer {hub_key}"})
        except Exception as exc:
            say(f"poll#{i} err {type(exc).__name__}")
            continue
        if isinstance(poll, dict):
            s = poll.get("status")
            if s == "ready":
                say(f"READY {json.dumps(poll.get('account') or {}, ensure_ascii=False)[:160]}")
                return True, "ready"
            if s in ("failed", "expired"):
                return False, f"poll {s}: {str(poll.get('message'))[:150]}"
    return False, "poll timeout"
