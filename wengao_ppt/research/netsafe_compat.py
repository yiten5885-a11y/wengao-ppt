"""wengao_ppt.research.netsafe_compat

网络请求安全层：
- 严格禁止触碰 8821 端口（硬约束）
- 本地 loopback 自动绕过系统代理 (ProxyHandler({}))
- 遵循 fake-IP 兼容规范：严禁使用预解析 DNS 判断公网，仅对字面量 IP 及不可信域名后缀过滤
- 限制单次抓取大小和超时时间
"""

from __future__ import annotations

import ipaddress
import urllib.parse
import urllib.request
from typing import Optional

FORBIDDEN_PORTS = {8821}
ALLOWED_LOOPBACK_PORTS = {8890, 11434, 18888, 8000, 8080}
MAX_FETCH_SIZE = 2 * 1024 * 1024  # 2MB 限制
DEFAULT_TIMEOUT_S = 8

UNTRUSTED_HOST_SUFFIXES = (
    ".local",
    ".internal",
    ".lan",
    ".corp",
    ".home",
    ".arpa",
)


class NetworkSecurityError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"[{code}] {detail}")
        self.code = code
        self.detail = detail


def is_loopback_host(host: str) -> bool:
    h = host.lower().strip()
    return h in ("127.0.0.1", "localhost", "::1")


def validate_target_url(url: str, allow_loopback: bool = False) -> urllib.parse.ParseResult:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise NetworkSecurityError("NET.INVALID_SCHEME", f"仅支持 HTTP/HTTPS 协议: {url}")

    if parsed.username or parsed.password:
        raise NetworkSecurityError("NET.EMBEDDED_CREDENTIALS", f"严禁 URL 中包含明文凭证: {url}")

    port = parsed.port
    if port is None:
        port = 80 if parsed.scheme == "http" else 443

    if port in FORBIDDEN_PORTS:
        raise NetworkSecurityError("NET.FORBIDDEN_PORT", f"严禁访问受保护端口 {port}")

    hostname = (parsed.hostname or "").strip().rstrip(".")
    if not hostname:
        raise NetworkSecurityError("NET.EMPTY_HOST", f"无效的目标主机名: {url}")

    if is_loopback_host(hostname):
        if not allow_loopback:
            raise NetworkSecurityError("NET.LOOPBACK_DISALLOWED", f"当前请求不允许访问本地环回地址: {url}")
        if port not in ALLOWED_LOOPBACK_PORTS:
            raise NetworkSecurityError("NET.UNAPPROVED_LOOPBACK_PORT", f"未批准的本地端口: {port}")
        return parsed

    # 外部目标必须为合法域名或非私有 IP，严格兼容 fake-IP 环境（不依赖 socket.gethostbyname）
    lowered = hostname.casefold()
    if lowered.endswith(UNTRUSTED_HOST_SUFFIXES):
        raise NetworkSecurityError("NET.UNTRUSTED_HOST", f"不允许访问内部专用后缀域名: {hostname}")

    try:
        ip = ipaddress.ip_address(lowered)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise NetworkSecurityError("NET.SSRF_BLOCKED", f"严禁直接访问私有/保留 IP 地址: {hostname}")
    except ValueError:
        # 非 IP 字面量，检查是否为规范的公网域名（包含点且非单段内网主机名）
        if "." not in lowered:
            raise NetworkSecurityError("NET.BARE_HOST", f"不允许单段内部主机名: {hostname}")

    return parsed


def safe_urlopen(
    url: str,
    data: Optional[bytes] = None,
    headers: Optional[dict[str, str]] = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    allow_loopback: bool = False,
    max_size: int = MAX_FETCH_SIZE,
) -> bytes:
    parsed = validate_target_url(url, allow_loopback=allow_loopback)
    req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)

    # 环回请求必须使用 ProxyHandler({}) 绕过系统环境变量代理
    if is_loopback_host(parsed.hostname or ""):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    else:
        opener = urllib.request.build_opener()

    with opener.open(req, timeout=timeout_s) as response:
        content = response.read(max_size + 1)
        if len(content) > max_size:
            content = content[:max_size]
        return content
