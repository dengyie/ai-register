"""z.ai 注册网名生成与唯一性预检。

反模式（2026-11-16 用户点名）：`zcoderegN` 顺序化前缀跨号可聚类。
正解：名字感词根组合（briarlow17 / felixgrant26 同款风格）。
铁律（2026-11-16 点号黑洞事故）：mailbox 本地部分只允许 ``[a-z0-9]`` ——
one-mail ``new_address`` 写入侧会静默剥离非字母数字字符（DEFAULT_NAME_REGEX），
带点/下划线 name 落库后与发件地址错位，验证信对 JWT 侧不可见。
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Any

UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}

HANDLE_RE = re.compile(r"^[a-z][a-z0-9]{2,29}$")

FIRST = [
    "ash", "briar", "felix", "miles", "hazel", "rowan", "wren", "luna", "sage",
    "iris", "fern", "moss", "ember", "quill", "nova", "juniper", "cinder",
    "opal", "tamsin", "casper", "odette", "perrin", "sable", "torren",
    "vesper", "yarrow", "muse", "nix", "sorrel", "wisp", "linden",
    "marlo", "petra", "ruan", "selkie", "thistle", "umber", "vanya", "wynn",
]
LAST = [
    "low", "grant", "muse", "ashford", "blackwood", "marlowe", "thorn",
    "vance", "holloway", "kettle", "finch", "quince", "alder", "bramble",
    "cove", "dune", "fallow", "grove", "harrow", "isle", "merriweather",
    "orchard", "penny", "reeve", "sallow", "tarn", "vale", "wick", "yarn",
    "ashby", "briarly", "cottle", "dovers", "elswich", "fenwick", "garrow",
]

PATTERNS = [
    ("{f}{l}{n2}", 50),   # briarlow17
    ("{l}{f}{n2}", 25),   # finchmiles08
    ("{f}{n2}", 15),      # ember63
    ("{f}{l}", 10),       # wrenthorn
]


def _weighted(patterns: list[tuple[str, int]], rnd: Any) -> str:
    total = sum(w for _, w in patterns)
    r = rnd.uniform(0, total)
    for pat, w in patterns:
        r -= w
        if r <= 0:
            return pat
    return patterns[0][0]


def generate_handle(rnd: Any) -> str:
    """生成一个自然感、纯 [a-z0-9] 网名（调用方负责唯一性查重）。"""
    import random

    f, l = rnd.choice(FIRST), rnd.choice(LAST)
    while l == f:  # FIRST/LAST 都有 muse 之类的词，避免 museemuseNN
        l = rnd.choice(LAST)
    handle = _weighted(PATTERNS, rnd).format(f=f, l=l, n2=f"{rnd.randint(1, 99):02d}")
    if not HANDLE_RE.fullmatch(handle):
        handle = f"{f}{rnd.randint(10, 99)}"
    return handle


def hub_label(handle: str, domain: str) -> str:
    """池内号名约定：<handle>-<域名去点>（zcodereg4-dengyietech 同款）。"""
    return f"{handle}-{domain.replace('.', '')}"


def _http_json(url: str, headers: dict[str, str], timeout: int = 20) -> Any:
    h = dict(UA)
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(req, timeout=timeout) as r:
        return json.load(r)


def onemail_address_exists(base_url: str, admin_key: str, email: str) -> bool:
    """one-mail 地址表精确查重（/admin/address?query= 前端过滤后客户端全等）。"""
    url = f"{base_url}/admin/address?query={urllib.parse.quote(email)}&limit=5&offset=0"
    d = _http_json(url, {"x-admin-auth": admin_key})
    rows = d.get("results") or d.get("rows") or (d if isinstance(d, list) else [])
    return any((r.get("name") or "").lower() == email.lower() for r in rows)


def hub_label_exists(hub_url: str, hub_key: str, label: str) -> bool:
    d = _http_json(f"{hub_url}/admin/api/accounts",
                   {"Authorization": f"Bearer {hub_key}"})
    accts = d if isinstance(d, list) else d.get("accounts") or d.get("data") or []
    return any((a.get("name") or "") == label for a in accts)


def generate_unique_handle(
    domain: str,
    *,
    onemail_base_url: str,
    onemail_admin_key: str,
    hub_url: str,
    hub_key: str,
    max_tries: int = 50,
    rnd: Any | None = None,
) -> str:
    """生成网名并对 one-mail 地址表 / hub 池 label 双查重；全撞则抛 ProviderError。"""
    import random

    from register_core.errors import FailFastError

    rnd = rnd or random.Random()
    for _ in range(max_tries):
        h = generate_handle(rnd)
        email = f"{h}@{domain}"
        label = hub_label(h, domain)
        if onemail_address_exists(onemail_base_url, onemail_admin_key, email):
            continue
        if hub_label_exists(hub_url, hub_key, label):
            continue
        return h
    raise FailFastError(
        f"naming: {max_tries} 个候选网名全部撞唯一性预检——词库告急或查询异常")
