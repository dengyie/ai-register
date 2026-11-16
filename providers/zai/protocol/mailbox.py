"""one-mail 收信读取（z.ai 验证链接专用）——unified admin API 直读。

设计要点（2026-11-16 点号事故教训）：
- 地址分配 = 纯发明（catch-all 域任意前缀直达入库，无需 new_address 行）；
  读取侧按 ``to_addr`` 客户端过滤，与地址行/JWT 完全解耦——one-mail 写入侧
  即便静默改写 name 也不影响本读取路径。
- 服务端 ``domain=`` 过滤（admin-only，auth_scope.ts）把轮询窗口从全站收窄
  到单域，防御洪峰下滑出 ``limit`` 窗口。
- 铁律：verify_email 链接必须全参数（token+email+username+language），
  缺参数 = Invalid Parameters 空表单；提取要吃 ``&amp;`` 实体。
- admin 通道 ``addr`` 参数不生效（管理员无租户过滤），必须客户端过滤。
- ``limit`` 上限 100（>100 报 quota_exceeded maxUnifiedPageSize）。
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
import urllib.request
from typing import Any

UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}

VERIFY_LINK_RE = re.compile(r"https://chat\.z\.ai/auth/verify_email\?[^\"'\\ <>]+")
FULL_PARAM_KEYS = ("token=", "email=", "username=", "language=")
LIST_LIMIT = 50  # ≤ maxUnifiedPageSize(100)


def is_full_verify_link(link: str) -> bool:
    return all(k in link for k in FULL_PARAM_KEYS)


def extract_verify_link(blob: str) -> str | None:
    """从邮件 JSON 原文中提取最新一条全参数 verify_email 链接（吃 &amp; 实体）。"""
    blob = html.unescape(blob)
    hits = VERIFY_LINK_RE.findall(blob)
    full = [h for h in hits if is_full_verify_link(h)]
    return full[-1] if full else None


def _get_json(url: str, headers: dict[str, str], timeout: int = 20,
              raw: bool = False) -> Any:
    req = urllib.request.Request(url, headers=headers)
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(req, timeout=timeout) as r:
        payload = r.read().decode()
    if raw:
        return payload
    return json.loads(payload)


class OneMailLinkMailbox:
    """发明一个 catch-all 地址并按 to_addr 轮询 z.ai 验证链接。"""

    name = "one_mail_link"

    def __init__(self, *, base_url: str, admin_key: str, domain: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.admin_key = admin_key
        self.domain = domain.strip()

    def allocate(self, email: str) -> str:
        """catch-all 域任意前缀直达入库——发明即分配，无服务端调用。"""
        return email

    def _headers(self) -> dict[str, str]:
        return {"x-admin-auth": self.admin_key, **UA}

    def _list_page(self) -> list[dict[str, Any]]:
        url = f"{self.base_url}/api/unified/emails?limit={LIST_LIMIT}"
        if self.domain:
            url += f"&domain={urllib.parse.quote(self.domain)}"
        d = _get_json(url, self._headers())
        return d.get("results") or []

    def fetch_body(self, email_id: str) -> str:
        return _get_json(
            f"{self.base_url}/api/unified/emails/{email_id}", self._headers(),
            raw=True)

    def scan_existing_verify_links(self, email: str) -> set[str]:
        """当前该地址名下已存在的全部全参数验证链接（作 baseline，只跑一次）。"""
        target = email.lower()
        out: set[str] = set()
        try:
            rows = self._list_page()
        except Exception:
            return out
        for m in rows:
            if (m.get("to_addr") or "").strip().lower() != target:
                continue
            if "verify" not in (m.get("subject") or "").lower():
                continue
            try:
                blob = self.fetch_body(m["id"])
            except Exception:
                continue
            out.update(h for h in VERIFY_LINK_RE.findall(html.unescape(blob))
                       if is_full_verify_link(h))
        return out

    def wait_verify_link(
        self,
        email: str,
        *,
        baseline: set[str] | None = None,
        timeout_s: float = 150,
        poll_interval_s: float = 8.0,
        log=None,
    ) -> str | None:
        """轮询本地址（to_addr 客户端全等过滤）在 baseline 之外的全参数验证链接。

        baseline = 调用开始前已存在的链接集合：只认 baseline 之外的新链接，
        同地址重跑不会把旧信误当收据。已检查过的邮件 id 不再重复拉 body
        （其链接要么在 baseline、要么不全参，重查无增量信息）。超时返回 None
        （调用方映射 mail_miss）。
        """
        baseline = set(baseline or set())
        target = email.lower()
        checked_ids: set[str] = set()
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                rows = self._list_page()
            except Exception as exc:  # 网络抖动不终止轮询
                if log:
                    log(f"mailbox list err: {type(exc).__name__}")
                time.sleep(poll_interval_s)
                continue
            for m in rows:
                if (m.get("to_addr") or "").strip().lower() != target:
                    continue
                if "verify" not in (m.get("subject") or "").lower():
                    continue
                mail_id = m.get("id")
                if not mail_id or mail_id in checked_ids:
                    continue
                if (m.get("received_at") or 0) <= 0:
                    continue
                try:
                    body = self.fetch_body(mail_id)
                except Exception as exc:
                    if log:
                        log(f"mail body fetch err {mail_id}: {type(exc).__name__}")
                    continue
                checked_ids.add(mail_id)
                link = extract_verify_link(body)
                if link and link not in baseline and is_full_verify_link(link):
                    return link
            time.sleep(poll_interval_s)
        return None
