"""来源链接清理：去掉跟踪参数和页内片段（#...），用于展示和去重。"""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PREFIXES = ("utm_", "hubs_", "mc_", "pk_", "_hs")
TRACKING_PARAMS = {
    "fbclid", "gclid", "dclid", "msclkid", "yclid", "igshid", "irclickid", "irgwc", "afsrc", "g_ref",
    "ref", "ref_src", "referrer", "lid", "ppcid", "origination", "originationcontext", "transitiontype",
    "contextdata", "viewtype", "_ga", "_gl", "share", "spm", "cmpid", "cid",
}


def clean_url(url: str) -> str:
    """去掉跟踪参数和片段；其他参数原样保留（有些网站靠参数定位文章）。无法解析时原样返回。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return url
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith(TRACKING_PREFIXES)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
