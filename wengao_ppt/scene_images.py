"""License-filtered photo search and per-job image caching for semantic scenes.

Only neutral English visual queries leave the local planner. Slide text, quotations,
page titles, metrics, and notes are never passed to a photo provider. Built-in
providers accept CC0/Public Domain Mark/Public Domain, commercial CC BY 4.0, or an
opt-in Pexels API result with a verified creator/source and canonical Pexels license
link. Source and license metadata stay in the frozen plan.
"""
from __future__ import annotations

import hashlib
import html
import http.client
import io
import ipaddress
import json
import math
import os
import socket
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

try:
    from PIL import Image
except ImportError:
    Image = None


class SceneImageError(RuntimeError):
    def __init__(self, code: str, detail: str, diagnostics: dict | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = str(detail)[:500]
        # Metadata only (mirrors SceneModelError.diagnostics): never a prompt or
        # a raw model response. Carries the review verdict so a rejected image
        # stays auditable after its bytes are discarded.
        self.diagnostics = dict(diagnostics or {})

    def as_record(self) -> dict:
        record = {"code": self.code, "detail": self.detail}
        if self.diagnostics:
            record["diagnostics"] = dict(self.diagnostics)
        return record


MAX_IMAGE_BYTES = 24 * 1024 * 1024
MIN_IMAGE_WIDTH = 600
MIN_IMAGE_HEIGHT = 360
MAX_QUERY_CHARS = 180
ALLOWED_FORMATS = {"JPEG": ".jpg", "PNG": ".png"}
MIN_METADATA_RELEVANCE = 0.5
SCENE_SEARCH_MAX_VARIANTS = 6
SCENE_SEARCH_PROVIDER_BUDGET_S = 12.0

#: Failures that describe the transport or the service, not the pictures.
#: They must never be reported as "no licensed photo": that sentence blames
#: the material and hides a network/proxy/model-independent outage.
_TRANSPORT_FAILURE_CODES = {
    "SCENE.PROVIDER_TLS_HANDSHAKE_TIMEOUT", "SCENE.PROVIDER_HTTP_ERROR",
    "SCENE.PROVIDER_UNAVAILABLE", "SCENE.PROVIDER_RATE_LIMITED",
    "SCENE.PROVIDER_AUTH_FAILED", "SCENE.PROVIDER_URL_INVALID",
    "SCENE.PROVIDER_RESPONSE_TOO_LARGE", "SCENE.SEARCH_BUDGET_EXHAUSTED",
    "SCENE.IMAGE_TLS_HANDSHAKE_TIMEOUT", "SCENE.IMAGE_FETCH_FAILED",
    "SCENE.IMAGE_RATE_LIMITED", "SCENE.IMAGE_REDIRECT_BLOCKED",
    "SCENE.IMAGE_EMPTY", "SCENE.IMAGE_URL_INVALID",
    "SCENE.NO_IMAGE_PROVIDER",
}
SCENE_SEARCH_QUERY_TIMEOUT_S = 4.0
#: Measured on the current WorkBuddy host (2026-10-23): Wikimedia Commons
#: query latency was about 5.0-5.3 s; a 4 s socket bound failed while 8 s
#: returned candidates. Openverse cache hits can be quick, but uncached queries
#: took 5-35 s. These are per-request client bounds, not global network changes.
#: 2026-10-28 on the same host behind a slower proxy route: every new HTTPS
#: connection to Pexels/Wikimedia spent 5-10 s in the TLS handshake alone
#: (Google 0.35 s), so a request finished in 8-16 s. Connections are now reused
#: per host (see _KeepAliveHTTPSHandler), so only the first request to each
#: provider pays the handshake; these bounds leave room for that first one.
#: Kept moderate on purpose: six scenes x three providers must still fit the
#: workbench "run" stage, and a 30 s x 2-attempt Openverse stall per scene
#: pushed a whole run past it.
SCENE_SEARCH_QUERY_TIMEOUT_BY_PROVIDER = {"openverse": 20.0,
                                          "wikimedia-commons": 15.0,
                                          "pexels": 20.0,
                                          "pixabay": 20.0}
SCENE_SEARCH_BUDGET_BY_PROVIDER = {"openverse": 45.0,
                                   "wikimedia-commons": 30.0,
                                   "pexels": 45.0,
                                   "pixabay": 45.0}

# Provider search can return a licensed but misleading result. Do not insert
# material whose own metadata signals explicit or graphic content. This is a
# conservative metadata guard, not a substitute for visual review.
_UNSAFE_METADATA_PATTERNS = (
    re.compile(r"\b(?:porn(?:ographic)?|erotic|fetish|nsfw|nudity|nude\s+(?:person|people|woman|man|figure|body|bodies|model|photo|photograph|art|painting)|"
               r"naked\s+(?:person|people|woman|man|body|bodies)|sexually\s+explicit|sex\s+act|lingerie)\b", re.I),
    re.compile(r"\b(?:graphic\s+(?:violence|injury|medical)|gore|corpse|autopsy|mutilat\w*|"
               r"severed\s+(?:limb|head|body)|open\s+wound|dead\s+body|suicide|self[- ]harm)\b", re.I),
)


def _strip_html(value) -> str:
    class Text(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []

        def handle_data(self, data):
            self.parts.append(data)

    parser = Text()
    parser.feed(str(value or ""))
    return re.sub(r"\s+", " ", html.unescape(" ".join(parser.parts))).strip()


def _license_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


_ALLOWED_LICENSE_KEYS = {
    "cc0", "cc0 1 0", "cc0 1.0", "cc0 1 0 universal",
    "pdm", "pdm 1 0", "public domain", "public domain mark",
    "public domain mark 1 0",
}
_CC_BY_4_LABELS = {"by", "cc by", "cc by 4 0", "cc by 4.0",
                   "cc by attribution 4 0", "attribution 4 0 international"}


def _is_allowed_license(value: str, *, version: str | None = None,
                        license_url: str | None = None) -> bool:
    key = _license_key(value)
    if key in ("pexels license", "pixabay license", "pixabay content license"):
        return bool(license_url) and is_valid_license_url(license_url, value)
    if key in _ALLOWED_LICENSE_KEYS:
        return not license_url or is_valid_license_url(license_url, value)
    if key not in _CC_BY_4_LABELS or not license_url:
        return False
    if key in {"by", "cc by"} and _license_key(version) != "4 0":
        return False
    if version and _license_key(version) != "4 0":
        return False
    return is_valid_license_url(license_url, "CC BY 4.0")


def _safe_query(query: str) -> str:
    q = re.sub(r"[\x00-\x1f\x7f]+", " ", str(query or ""))
    q = re.sub(r"\s+", " ", q).strip()
    if not q or len(q) > MAX_QUERY_CHARS:
        raise SceneImageError("SCENE.QUERY_INVALID", "场景照片查询为空或超过长度限制。")
    if re.search(r"https?://|www\.|[\r\n]", q, re.I):
        raise SceneImageError("SCENE.QUERY_INVALID", "照片查询不能包含链接或换行。")
    return q


def is_neutral_photo_search_query(query: object) -> bool:
    """Validate a short English image-search reformulation.

    Reformulations broaden provider recall only. Their results are still checked
    against the scene's original structured subject/action/setting anchors.
    """
    try:
        q = _safe_query(str(query or ""))
    except SceneImageError:
        return False
    if not re.fullmatch(r"[A-Za-z][A-Za-z '\\-]*", q):
        return False
    terms = _query_tokens(q)
    return 4 <= len(terms) <= 7


def _url_is_allowed(url: str, allowed_hosts: set[str]) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        allowed = {str(item).casefold().rstrip(".") for item in allowed_hosts}
        return (parsed.scheme.casefold() == "https"
                and parsed.port in (None, 443)
                and host in allowed
                and not parsed.username and not parsed.password)
    except Exception:
        return False


_UNTRUSTED_HOST_SUFFIXES = (".local", ".internal", ".localhost", ".home.arpa",
                            ".lan", ".intranet")


def _url_is_public_https(url: str) -> bool:
    """Return True for a public HTTPS URL that may be fetched.

    The check is deliberately hostname-shaped and does **not** require the
    name to resolve to a global IP. Proxy and VPN clients commonly answer DNS
    with a fake-IP address (every name maps into one private /16) so that all
    outbound traffic is routed through the proxy. A pre-resolution
    "must resolve to a public IP" test is unusable in that setup: it rejected
    every provider image, which silently dropped all candidates and surfaced
    as a misleading "no licensed photo" failure instead of a network fact.

    What is still refused: non-HTTPS schemes, embedded credentials,
    localhost/.local/.internal style names, bare single-label hosts, and
    literal private, loopback, link-local, multicast, reserved or unspecified
    IP addresses. Redirects are re-checked with the same rule, so a provider
    cannot bounce the fetcher onto an internal address literal.
    """
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").strip().rstrip(".")
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            return False
        lowered = host.casefold()
        if lowered == "localhost" or lowered.endswith(_UNTRUSTED_HOST_SUFFIXES):
            return False
        try:
            ip = ipaddress.ip_address(lowered)
        except ValueError:
            # A registrable-looking public hostname, not a bare intranet name.
            return "." in lowered
        return not (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_multicast or ip.is_reserved or ip.is_unspecified)
    except Exception:
        return False


def _host_resolution_report(host: str) -> dict:
    """Diagnostic only: report what DNS returns without gating any fetch.

    Fake-IP proxy setups answer every name with the same private range; this
    record makes that visible in job evidence instead of leaving a silent
    "unresolvable" impression.
    """
    try:
        records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except Exception as exc:  # noqa: BLE001 — diagnostic only
        return {"host": host, "resolved": False, "error": type(exc).__name__}
    addresses = sorted({row[4][0] for row in records})
    return {"host": host, "resolved": True, "addresses": addresses,
            "all_global": bool(addresses) and all(
                ipaddress.ip_address(address).is_global for address in addresses)}


#: Model-free photographic guard. Calibrated 2026-10-23 on two real downloads:
#: a Wikimedia line-art illustration measured 440 distinct 5-bit colours with
#: 79.5% of pixels in its eight most common colours, while a real photograph
#: measured 2682 colours and 20.2%. A scene image must be a photograph, so a
#: flat, low-colour image is rejected instead of being inserted as a "scene".
_FLAT_ART_MAX_UNIQUE_5BIT = 1200
_FLAT_ART_MIN_TOP8_FRACTION = 0.55


def photographic_asset_check(data: bytes) -> dict:
    """Return whether downloaded bytes look like a photograph, not flat artwork.

    Colour-count statistics are a cheap, deterministic proxy: vector art and
    line drawings are dominated by a handful of flat fills, while photographs
    carry thousands of distinct colours. The check runs on the actual
    downloaded bytes, so a misleading title cannot smuggle an illustration in.
    """
    if Image is None:
        return {"photographic": False, "reason": "PIL_UNAVAILABLE"}
    try:
        with Image.open(io.BytesIO(data)) as image:
            sample = image.convert("RGB").resize((256, 256))
        getter = getattr(sample, "get_flattened_data", None)
        pixels = list(getter() if getter else sample.getdata())
    except Exception:
        return {"photographic": False, "reason": "DECODE_FAILED"}
    quantized = [(r >> 3, g >> 3, b >> 3) for r, g, b in pixels]
    counts = Counter(quantized)
    total = max(1, len(quantized))
    top8 = sum(value for _, value in counts.most_common(8)) / total
    unique = len(counts)
    lumas = [0.299 * r + 0.587 * g + 0.114 * b for r, g, b in pixels]
    mean_y = sum(lumas) / total
    rms_contrast = math.sqrt(sum((y - mean_y) ** 2 for y in lumas) / total)
    dynamic_range = max(lumas) - min(lumas)
    contrast_insufficient = (rms_contrast < 10.0 or dynamic_range < 25.0)
    flat = (unique < _FLAT_ART_MAX_UNIQUE_5BIT
            and top8 > _FLAT_ART_MIN_TOP8_FRACTION)
    return {"photographic": not flat and not contrast_insufficient,
            "unique_5bit": unique,
            "top8_fraction": round(top8, 4),
            "rms_contrast": round(rms_contrast, 2),
            "dynamic_range": round(dynamic_range, 2),
            "contrast_insufficient": contrast_insufficient,
            "thresholds": {"unique_5bit": _FLAT_ART_MAX_UNIQUE_5BIT,
                           "top8_fraction": _FLAT_ART_MIN_TOP8_FRACTION,
                           "min_rms_contrast": 10.0,
                           "min_dynamic_range": 25.0}}


def perceptual_hash(data: bytes) -> str:
    """64-bit dHash for exact/re-encoded photo duplicate checks."""
    if Image is None:
        raise SceneImageError("SCENE.PIL_UNAVAILABLE", "PIL/Pillow 未安装，无法处理图片。")
    with Image.open(io.BytesIO(data)) as image:
        gray = image.convert("L")
    resized = gray.resize((9, 8))
    getter = getattr(resized, "get_flattened_data", None)
    pixels = list(getter() if getter else resized.getdata())
    difference = 0
    for y in range(8):
        row = pixels[y * 9:(y + 1) * 9]
        for x in range(8):
            difference = (difference << 1) | int(row[x] > row[x + 1])
    average_resized = gray.resize((8, 8))
    getter = getattr(average_resized, "get_flattened_data", None)
    average_pixels = list(getter() if getter else average_resized.getdata())
    average = sum(average_pixels) / len(average_pixels)
    average_hash = 0
    for pixel in average_pixels:
        average_hash = (average_hash << 1) | int(pixel >= average)
    return f"{difference:016x}{average_hash:016x}"


def image_hash_distance(a: str, b: str) -> int:
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except (TypeError, ValueError):
        return 65


def is_allowed_license(value: str, *, version: str | None = None,
                       license_url: str | None = None) -> bool:
    return _is_allowed_license(value, version=version, license_url=license_url)


def is_valid_license_url(value: str, license_name: str) -> bool:
    """Accept only license URLs that match the declared rights label.

    Wikimedia's machine-readable CC0/PDM records still return the official
    deed URLs over HTTP. Allow only canonical Creative Commons paths matching
    the declared rights label; arbitrary and mismatched HTTPS/HTTP links stay
    blocked. The page URL remains an attribution reference, not a fetch target.
    """
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        parsed = urlparse(raw)
        if parsed.username or parsed.password or not parsed.hostname:
            return False
        key = _license_key(license_name)
        if key == "pexels license":
            return (parsed.scheme == "https"
                    and parsed.hostname.lower() in {"pexels.com", "www.pexels.com"}
                    and parsed.port in (None, 443)
                    and parsed.path.rstrip("/").lower() == "/license"
                    and not parsed.query and not parsed.fragment)
        if key in ("pixabay license", "pixabay content license"):
            return (parsed.scheme == "https"
                    and parsed.hostname.lower() in {"pixabay.com", "www.pixabay.com"}
                    and parsed.port in (None, 443)
                    and parsed.path.rstrip("/").lower() == "/service/license-summary"
                    and not parsed.query and not parsed.fragment)
        if key in _CC_BY_4_LABELS:
            return (parsed.scheme in {"https", "http"}
                    and parsed.hostname.lower() in {"creativecommons.org", "www.creativecommons.org"}
                    and parsed.port in (None, 80, 443)
                    and parsed.path.rstrip("/").lower() == "/licenses/by/4.0")
        if (parsed.scheme not in {"http", "https"}
                or parsed.hostname.lower() not in {"creativecommons.org", "www.creativecommons.org"}
                or parsed.port not in (None, 80, 443)):
            return False
        path = parsed.path.rstrip("/").lower()
        if key in {"cc0", "cc0 1 0", "cc0 1.0", "cc0 1 0 universal"}:
            return path.startswith("/publicdomain/zero/")
        if key in {"pdm", "pdm 1 0", "public domain mark", "public domain mark 1 0"}:
            return path.startswith("/publicdomain/mark/")
        if key == "public domain":
            return path.startswith(("/publicdomain/mark/", "/publicdomain/zero/"))
        return False
    except (TypeError, ValueError):
        return False


def is_valid_pexels_source_page(value: str) -> bool:
    """Accept only a first-party Pexels photo page as source attribution."""
    try:
        parsed = urlparse(str(value or "").strip())
        return (parsed.scheme == "https"
                and parsed.hostname is not None
                and parsed.hostname.lower() in {"pexels.com", "www.pexels.com"}
                and parsed.port in (None, 443)
                and parsed.path.lower().startswith("/photo/")
                and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment)
    except (TypeError, ValueError):
        return False


def is_valid_pixabay_source_page(value: str) -> bool:
    """Accept only a first-party Pixabay photo page as source attribution."""
    try:
        parsed = urlparse(str(value or "").strip())
        return (parsed.scheme == "https"
                and parsed.hostname is not None
                and parsed.hostname.lower() in {"pixabay.com", "www.pixabay.com"}
                and parsed.port in (None, 443)
                and parsed.path.lower().startswith("/photos/")
                and not parsed.username and not parsed.password
                and not parsed.query and not parsed.fragment)
    except (TypeError, ValueError):
        return False


_QUERY_STOP_WORDS = {
    "the", "and", "with", "from", "into", "while", "photo", "image", "picture",
    "photograph", "photography", "photographic", "documentary", "wide", "horizontal",
    "landscape", "scene", "at", "in", "on", "of", "a", "an", "to", "realistic",
    "professional", "natural", "high", "resolution", "clear", "focus",
}

_SETTING_ALIASES = {
    "bedroom": {"bedroom", "bed", "sleeping", "wake", "waking"},
    "bathroom": {"bathroom", "washroom", "restroom", "sink", "mirror"},
    # A recording studio is not interchangeable with an office; treating it
    # as one made music-production archive photos pass office-work queries.
    "office": {"office", "workplace", "workstation", "desk"},
    "workplace": {"workplace", "office", "workstation", "factory", "worksite", "desk"},
    "factory": {"factory", "plant", "production", "assembly", "manufacturing"},
    "supermarket": {"supermarket", "grocery", "store", "checkout", "retail"},
    "grocery store": {"grocery store", "supermarket", "produce market", "food market"},
    "production line": {"production line", "assembly line", "factory floor", "manufacturing line"},
    "apartment doorway": {"apartment doorway", "apartment entrance", "home doorway",
                          "front door", "doorstep"},
    "school": {"school", "classroom", "teacher", "student", "campus"},
    "clinic": {"clinic", "hospital", "outpatient", "doctor", "medical"},
    "hospital": {"hospital", "clinic", "ward", "patient", "medical"},
    "classroom": {"classroom", "school", "teacher", "student", "lecture"},
    "desk": {"desk", "office", "workstation", "table"},
    "kitchen": {"kitchen", "home", "cooking", "counter"},
    "home": {"home", "house", "kitchen", "bedroom", "living"},
    "production": {"production", "factory", "plant", "manufacturing"},
    "assembly": {"assembly", "factory", "plant", "production"},
    "airport": {"airport", "terminal", "airfield"},
    "station": {"station", "subway", "railway", "train"},
    "restaurant": {"restaurant", "cafe", "dining", "food"},
    "library": {"library", "reading room", "bookstore"},
    "worksite": {"worksite", "construction", "building site"},
    "construction site": {"construction site", "building site", "worksite"},
    "building site": {"building site", "construction site", "worksite"},
    "farm": {"farm", "farmland", "field", "agricultural land"},
    "checkout": {"checkout", "cash register", "checkout counter", "register terminal"},
    "subway": {"subway", "underground", "metro"},
    "taxi": {"taxi", "cab", "taxicab"},
    "airport terminal": {"airport terminal", "terminal", "airport"},
    "work": {"work", "workplace", "workspace", "workstation", "office", "factory", "worksite"},
    "site": {"site", "worksite", "workplace", "service center", "service counter", "building site"},
    "service": {"service", "service center", "service counter", "service desk", "customer service"},
    "workspace": {"workspace", "workplace", "workstation", "office", "desk"},
    # Business/tech settings (2026-10-28, telecom strategy deck): Pexels titled a
    # perfect match "executives discussing strategies in a modern boardroom", but
    # "meeting room" had no aliases, so every such caption was discarded before
    # the visual review could judge the pixels.
    "meeting room": {"meeting room", "conference room", "boardroom", "board room",
                     "meeting", "office"},
    "conference room": {"conference room", "meeting room", "boardroom", "board room",
                        "meeting"},
    "boardroom": {"boardroom", "board room", "conference room", "meeting room"},
    "corporate office": {"corporate office", "office", "workplace", "boardroom",
                         "conference room"},
    "data center": {"data center", "datacenter", "server room", "server rack",
                    "server racks", "servers"},
    "datacenter": {"datacenter", "data center", "server room", "server racks"},
    "server room": {"server room", "data center", "datacenter", "server racks"},
    "control room": {"control room", "operations center", "monitoring room",
                     "command center"},
    "living room": {"living room", "lounge", "sofa", "couch", "home"},
    "retail store": {"retail store", "store", "shop", "retail", "supermarket",
                     "boutique", "checkout"},
    "store": {"store", "shop", "retail", "supermarket", "boutique"},
    "shop": {"shop", "store", "retail", "boutique"},
    "city": {"city", "urban", "downtown", "street", "skyline"},
}

# Provider captions often use a concrete equivalent rather than the exact words
# chosen by the local planner. These small role-specific vocabularies widen
# retrieval while the gate still requires subject, action/object, and place to
# be supported together by one title/caption unit. They are intentionally not
# broad topic labels: for example, a recording studio is never an office alias.
_SUBJECT_ALIASES = {
    "person": {"person", "people", "human", "adult", "woman", "man", "child", "boy", "girl",
               "student", "patient", "employee", "worker", "customer", "rider"},
    "people": {"person", "people", "human", "adult", "woman", "man", "child", "student"},
    "adult": {"adult", "person", "human", "woman", "man", "worker", "patient", "student"},
    "staff": {"staff", "employee", "worker", "personnel", "clerk", "crew"},
    "employee": {"employee", "staff", "worker", "personnel", "clerk"},
    "worker": {"worker", "employee", "staff", "laborer", "operator"},
    "factory worker": {"factory worker", "production worker", "assembly worker",
                       "industrial worker"},
    "production worker": {"production worker", "factory worker", "assembly worker",
                          "industrial worker"},
    "consumer": {"consumer", "customer", "shopper", "buyer", "parent"},
    # A store caption rarely says "customer": it says who is shopping. The
    # action and setting anchors still have to match in the same caption.
    "customer": {"customer", "consumer", "shopper", "buyer", "person", "woman", "man"},
    "shopper": {"shopper", "customer", "consumer", "buyer", "parent"},
    "student": {"student", "pupil", "learner", "child"},
    "doctor": {"doctor", "physician", "clinician"},
    "physician": {"physician", "doctor", "clinician"},
    "nurse": {"nurse", "caregiver", "clinician"},
    "rider": {"rider", "courier", "delivery worker", "delivery driver"},
    "courier": {"courier", "delivery rider", "delivery worker", "delivery driver",
                "food courier"},
    "construction worker": {"construction worker", "builder", "site worker", "laborer"},
    "farmer": {"farmer", "farm worker", "agricultural worker", "field worker"},
    "office worker": {"office worker", "office employee", "desk worker", "employee"},
    "commuter": {"commuter", "subway passenger", "train passenger", "traveler"},
    "traveler": {"traveler", "tourist", "passenger", "visitor"},
    "elderly person": {"elderly person", "older adult", "senior", "older person"},
    "older adult": {"older adult", "older adults", "senior", "seniors",
                    "elderly person", "elderly people", "older person", "older people"},
    "older adults": {"older adult", "older adults", "senior", "seniors",
                     "elderly person", "elderly people", "older person", "older people"},
    "cup": {"cup", "mug", "drinking glass"},
    "mug": {"mug", "cup", "travel mug", "coffee mug"},
    "smartphone": {"smartphone", "mobile phone", "cell phone", "iphone"},
    "laptop": {"laptop", "notebook computer", "macbook"},
    # Business/tech roles (2026-10-28): stock captions say "professionals",
    # "businesswoman" or "team" far more often than the planner's exact noun.
    "executive": {"executive", "executives", "business leader", "businessman",
                  "businesswoman", "businesspeople", "business people", "manager",
                  "director", "business professional", "business professionals"},
    "executives": {"executives", "executive", "business leaders", "businessmen",
                   "businesswomen", "businesspeople", "business people", "managers",
                   "directors", "business professionals", "professionals"},
    "manager": {"manager", "executive", "supervisor", "team leader", "businessman",
                "businesswoman"},
    "professional": {"professional", "professionals", "businessman", "businesswoman",
                     "businessperson", "office worker", "employee"},
    "professionals": {"professionals", "professional", "businesspeople",
                      "business people", "colleagues", "coworkers", "team"},
    "team": {"team", "colleagues", "coworkers", "professionals", "staff",
             "group of people"},
    "engineer": {"engineer", "technician", "it specialist", "it professional",
                 "network engineer", "system administrator"},
    "technician": {"technician", "engineer", "it specialist", "it professional",
                   "repairman"},
    "developer": {"developer", "programmer", "software engineer", "coder",
                  "software developer"},
    "programmer": {"programmer", "developer", "software engineer", "coder"},
    "specialist": {"specialist", "expert", "analyst", "consultant", "professional"},
    "analyst": {"analyst", "data analyst", "specialist", "professional"},
    "user": {"user", "customer", "consumer", "person", "man", "woman"},
}

_SUBJECT_SEARCH_VARIANTS = {
    "person": ("woman", "man", "adult"),
    "people": ("woman", "man", "adult"),
    "young adult": ("young woman", "young man", "young person", "adult"),
    "adult": ("woman", "man", "person"),
    "staff": ("employee", "worker", "clerk"),
    "employee": ("staff", "worker", "clerk"),
    "worker": ("employee", "staff", "operator"),
    "consumer": ("shopper", "customer", "buyer"),
    "customer": ("shopper", "consumer", "buyer"),
    "shopper": ("grocery shopper", "customer", "consumer"),
    "factory worker": ("production worker", "assembly worker", "industrial worker"),
    "production worker": ("factory worker", "assembly worker", "industrial worker"),
    "construction worker": ("builder", "site worker", "construction laborer"),
    "farmer": ("farm worker", "agricultural worker", "field worker"),
    "office worker": ("office employee", "desk worker", "staff"),
    "commuter": ("subway passenger", "train passenger", "office commuter"),
    "traveler": ("tourist", "passenger", "visitor"),
    "elderly person": ("older adult", "senior", "older person"),
    "cup": ("ceramic cup", "mug", "drinking glass"),
    "mug": ("ceramic mug", "travel mug", "cup"),
    "smartphone": ("mobile phone", "cell phone", "iphone"),
    "laptop": ("notebook computer", "macbook", "computer"),
    "courier": ("delivery rider", "delivery worker", "food courier"),
    "parent": ("family shopper", "grocery shopper", "customer"),
    "older adult": ("senior", "elderly person", "older person"),
    "older adults": ("seniors", "elderly people", "older people"),
    "student": ("pupil", "learner", "child"),
    "students": ("pupils", "schoolchildren", "learners"),
    "doctor": ("physician", "clinician"),
    "nurse": ("caregiver", "clinician"),
    "rider": ("courier", "delivery worker"),
}

_ACTION_PHRASE_ALIASES = {
    "stretching arms": (
        "stretching arms", "morning stretch", "arms raised", "raising arms", "stretching",
    ),
    "brushing teeth": (
        "brushing teeth", "brush teeth", "tooth brushing", "oral hygiene", "toothbrush",
    ),
    "verifying task requirements": (
        "verifying task requirements", "checking requirements", "reviewing checklist",
        "checking task list", "inspecting requirements", "confirming requirements",
    ),
    "recording completion status": (
        "recording completion status", "recording task completion", "documenting completed tasks",
        "logging task completion", "marking checklist complete",
    ),
    "selecting produce": (
        "selecting produce", "choosing produce", "selecting vegetables",
        "shopping for produce", "shopping for groceries", "buying vegetables",
        "choosing groceries",
    ),
    "assembling products": (
        "assembling products", "assembling parts", "assembly work",
        "building products", "assembling components",
    ),
    "carrying food": (
        "carrying food", "delivering food", "carrying meals",
        "food delivery", "delivering groceries", "bringing meals",
    ),
    "reading lesson": (
        "reading lesson", "studying lesson", "classroom learning",
        "learning lesson", "reading in class",
    ),
    "consulting patient": (
        "consulting patient", "examining patient", "seeing patient",
        "treating patient", "meeting patient",
    ),
    "choosing groceries": (
        "choosing groceries", "selecting groceries", "shopping for groceries",
        "choosing produce", "selecting produce", "buying food",
    ),
    "boarding subway": (
        "boarding subway", "boarding metro", "boarding underground train",
        "taking subway", "commuting subway",
    ),
    # "out" is a stop word, so "checking out" could never satisfy the generic
    # verb+object rule; stock captions describe it as paying at a counter.
    "checking out": (
        "checking out", "checkout", "contactless payment", "paying", "payment",
        "cashier", "cash register", "pays",
    ),
    "paying bill": (
        "paying bill", "paying bills", "pay bills", "online payment", "paying online",
        "payment", "paying",
    ),
}

_ING_DROP_E_STEMS = {
    "us": "use", "mak": "make", "tak": "take", "giv": "give",
    "driv": "drive", "rid": "ride", "writ": "write", "hik": "hike",
    "bik": "bike", "mov": "move", "serv": "serve", "smil": "smile",
    "ly": "lie",
}

_SETTING_SEARCH_VARIANTS = {
    "bathroom": ("mirror", "sink", "washroom"),
    "bedroom": ("bed", "sleeping room"),
    "office": ("workstation", "desk", "workplace"),
    "workplace": ("office", "workstation", "desk"),
    "service site": ("service counter", "service desk", "service center", "office"),
    "work area": ("office", "workstation", "desk", "workplace"),
    "factory": ("manufacturing plant", "production line"),
    "school": ("classroom", "campus"),
    "classroom": ("school", "lecture room"),
    "clinic": ("hospital", "medical clinic"),
    "hospital": ("clinic", "medical ward"),
    "supermarket": ("grocery store", "checkout"),
    "grocery store": ("supermarket", "produce market", "food market"),
    "production line": ("assembly line", "factory floor", "manufacturing line"),
    "apartment doorway": ("apartment entrance", "front door", "doorstep"),
    "restaurant": ("cafe", "dining room"),
    "library": ("reading room", "bookstore"),
    "airport": ("terminal", "airfield"),
    "station": ("subway station", "railway station"),
    "construction site": ("building site", "worksite", "job site"),
    "building site": ("construction site", "worksite", "job site"),
    "farm": ("farmland", "crop field", "agricultural land"),
    "checkout": ("checkout counter", "cash register", "supermarket"),
    "subway": ("metro station", "underground station", "train station"),
    "taxi": ("cab", "taxicab", "city street"),
    "airport terminal": ("terminal", "airport"),
}

_PHOTO_ARTWORK_RE = re.compile(
    r"\b(illustration|illustrated|engraving|painting|drawing|sketch|comic|cartoon|diagram|"
    r"screenshot|poster|woodcut|lithograph)\b", re.IGNORECASE)


def _query_tokens(query: str) -> list[str]:
    return list(dict.fromkeys(
        token for token in re.findall(r"[a-z0-9]+", str(query or "").lower())
        if len(token) >= 3 and token not in _QUERY_STOP_WORDS))


def _query_terms(query: str) -> set[str]:
    return set(_query_tokens(query))


def normalize_photo_search_terms(value: object) -> dict[str, str]:
    """Validate explicit English subject/action/place anchors for one scene.

    The model may describe the scene in any source language, but photo search
    receives this small, role-separated English vocabulary. Keeping the roles
    separate lets the metadata gate distinguish an action object from a place
    and reject a coincidental broad-word hit.
    """
    if not isinstance(value, dict):
        raise ValueError("photo_search_terms must be an object")
    result = {}
    bounds = {"subject": (1, 2), "action": (2, 4), "setting": (1, 2)}
    for key, (minimum, maximum) in bounds.items():
        raw = value.get(key)
        if (not isinstance(raw, str) or not raw.strip() or len(raw) > 100
                or re.search(r"\d|[\u3400-\u9fff]", raw)):
            raise ValueError(f"{key} must contain short English search anchors")
        tokens = _query_tokens(raw)
        if not minimum <= len(tokens) <= maximum:
            raise ValueError(f"{key} must contain {minimum}-{maximum} useful English words")
        result[key] = " ".join(tokens)
    total = sum(len(_query_tokens(result[key])) for key in bounds)
    if not 4 <= total <= 7:
        raise ValueError("photo search anchors must total 4-7 useful words")
    groups = [set(_query_tokens(result[key])) for key in bounds]
    if any(groups[i] & groups[j] for i in range(len(groups)) for j in range(i + 1, len(groups))):
        raise ValueError("subject, action, and setting anchors must be distinct")
    generic_settings = {"location", "place", "setting", "environment", "scene", "area"}
    if set(_query_tokens(result["setting"])) <= generic_settings:
        raise ValueError("setting must name a concrete place or environment")
    return result


def canonicalize_duplicate_photo_search_terms(value: object) -> dict[str, str] | None:
    """Remove exact cross-role duplicate tokens in subject/action/setting order.

    This is a formatting-only repair: each token is kept in its first semantic
    role and later copies are dropped. Return a value only if the unchanged
    strict validator accepts every field and the final 4-7 word query.
    """
    if not isinstance(value, dict):
        return None
    result: dict[str, str] = {}
    seen: set[str] = set()
    found_duplicate = False
    for field in ("subject", "action", "setting"):
        raw = value.get(field)
        if (not isinstance(raw, str) or not raw.strip() or len(raw) > 100
                or re.search(r"\d|[\u3400-\u9fff]", raw)):
            return None
        kept = []
        for token in _query_tokens(raw):
            if token in seen:
                found_duplicate = True
                continue
            kept.append(token)
            seen.add(token)
        result[field] = " ".join(kept)
    if not found_duplicate:
        return None
    try:
        return normalize_photo_search_terms(result)
    except ValueError:
        return None


def query_preserves_photo_search_anchors(query: str, anchors: object) -> bool:
    """Accept only a compact ordered query retaining every role-separated anchor."""
    try:
        normalized = normalize_photo_search_terms(anchors)
    except ValueError:
        return False
    expected = [token for key in ("subject", "action", "setting")
                for token in _query_tokens(normalized[key])]
    actual = _query_tokens(query)
    subject = _query_tokens(normalized["subject"])
    action = _query_tokens(normalized["action"])
    setting = _query_tokens(normalized["setting"])
    if not 4 <= len(actual) <= 7 or len(actual) < len(expected):
        return False
    if actual[:len(subject) + len(action)] != subject + action:
        return False
    if actual[-len(setting):] != setting:
        return False
    # At most one extra neutral qualifier may refine the same scene between
    # the action and final place anchor; required role anchors stay untouched.
    extra_count = len(actual) - len(expected)
    return 0 <= extra_count <= 1


def build_provider_search_query_from_anchors(anchors: object) -> str:
    """Join already validated role-separated anchors without dropping or reordering them."""
    normalized = normalize_photo_search_terms(anchors)
    query = _safe_query(" ".join(
        normalized[field] for field in ("subject", "action", "setting")))
    if not query_preserves_photo_search_anchors(query, normalized):
        raise ValueError("normalized photo anchors did not survive query construction")
    return query


def _token_forms(token: str) -> set[str]:
    forms = {token}
    if len(token) >= 5 and token.endswith("ing"):
        stem = token[:-3]
        forms.add(stem[:-1] if len(stem) > 3 and stem[-1:] == stem[-2:-1] else stem)
        if stem in _ING_DROP_E_STEMS:
            forms.add(_ING_DROP_E_STEMS[stem])
    if len(token) > 4 and token.endswith("ied"):
        forms.add(token[:-3] + "y")
    if len(token) > 4 and token.endswith("ed"):
        forms.add(token[:-2])
    if len(token) > 4 and token.endswith("ies"):
        forms.add(token[:-3] + "y")
    if len(token) > 4 and token.endswith("es"):
        forms.add(token[:-2])
    if len(token) > 3 and token.endswith("s"):
        forms.add(token[:-1])
    return {form for form in forms if len(form) >= 3}


def _metadata_tokens(record: dict) -> set[str]:
    # Some archive pages expose the entire finding aid or newspaper article in
    # ImageDescription. It is not evidence about the depicted frame. Keep the
    # evidence window bounded so a distant word in a long transcription cannot
    # make an unrelated picture pass.
    title = str(record.get("title") or "")
    description = str(record.get("description") or "")[:1200]
    return set(re.findall(r"[a-z0-9]+", f"{title} {description}".lower()))


def _metadata_evidence_units(record: dict) -> list[set[str]]:
    """Keep semantic evidence local to the title and at most one caption sentence.

    Long archive descriptions frequently contain unrelated mission history or
    neighboring records. Combining the whole description let separate clauses
    accidentally supply the subject, action, and place for an unrelated image.
    The provider title and a single description sentence both describe the same
    image record, though, and either may hold the only location or subject word.
    Check each title/sentence pair, never a union of separate caption sentences.
    """
    title = _strip_html(record.get("title"))
    description = _strip_html(str(record.get("description") or "")[:1200])
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+|[;\n]+", description)
                 if part.strip()]
    units = [title] if title else []
    units.extend(sentences)
    if title:
        units.extend(f"{title}. {sentence}" for sentence in sentences)
    evidence = []
    for unit in units:
        tokens = set(re.findall(r"[a-z0-9]+", unit.lower()))
        for token in tuple(tokens):
            tokens.update(_token_forms(token))
        evidence.append(tokens)
    return evidence


def _phrase_supported(phrase: str, tokens: set[str]) -> bool:
    words = _query_tokens(phrase)
    return bool(words) and all(bool(_token_forms(word) & tokens) for word in words)


def _subject_supported(anchor: str, tokens: set[str]) -> bool:
    words = _query_tokens(anchor)
    phrase_key = " ".join(words)
    phrase_aliases = _SUBJECT_ALIASES.get(phrase_key, set())
    if any(_phrase_supported(candidate, tokens) for candidate in phrase_aliases):
        return True
    for word in words:
        candidates = _SUBJECT_ALIASES.get(word, {word})
        if any(_phrase_supported(candidate, tokens) for candidate in candidates):
            return True
    return False


def _action_supported(anchor: str, tokens: set[str]) -> bool:
    normalized = " ".join(_query_tokens(anchor))
    known = _ACTION_PHRASE_ALIASES.get(normalized)
    if known:
        return any(_phrase_supported(phrase, tokens) for phrase in known)
    terms = _query_tokens(anchor)
    if len(terms) < 2:
        return False
    # Unknown actions remain open-vocabulary, but must show a verb and an
    # object/result in the same caption unit.
    return (bool(_token_forms(terms[0]) & tokens)
            and any(bool(_token_forms(term) & tokens) for term in terms[1:]))


def _setting_supported(anchor: str, tokens: set[str]) -> bool:
    terms = _query_tokens(anchor)
    if not terms:
        return False
    if any(bool(_token_forms(term) & tokens) for term in terms):
        return True
    aliases = set().union(*(_SETTING_ALIASES.get(term, set()) for term in terms))
    aliases.update(_SETTING_ALIASES.get(" ".join(terms), set()))
    return any(_phrase_supported(alias, tokens) for alias in aliases)


def scene_search_query_variants(query: str, scene_context: dict | None = None,
                                *, max_variants: int = 6) -> list[str]:
    """Build bounded, role-preserving provider queries for natural caption wording."""
    base = _safe_query(query)
    anchors = scene_context.get("photo_search_terms") if isinstance(scene_context, dict) else None
    if anchors is None:
        return [base]
    try:
        roles = normalize_photo_search_terms(anchors)
    except ValueError:
        return [base]
    planned_queries = scene_context.get("visual_queries") if isinstance(scene_context, dict) else None
    if not isinstance(planned_queries, list):
        planned_queries = []
    # Planner alternatives are a search-recall aid only. The caller's canonical
    # metadata_query remains the authority for scene relevance, so a synonym can
    # find a candidate but cannot make an unrelated image pass the anchor gate.
    validated_queries = []
    # Model-authored variants help with provider vocabulary, but allowing three
    # alternatives plus the canonical query used to consume the entire provider
    # budget before any built-in role synonym ran. Reserve deterministic room
    # for those fallbacks so a fluent but provider-unfamiliar phrase (for
    # example "consumer paying at checkout") can also search "shopper ..."
    # and "cash register" wording. The canonical metadata gate remains the
    # authority; these query alternatives never weaken acceptance.
    variant_limit = max(1, int(max_variants))
    planner_alternative_limit = max(1, min(2, variant_limit - 2))
    for value in planned_queries[:planner_alternative_limit]:
        if isinstance(value, str) and is_neutral_photo_search_query(value.strip()):
            validated_queries.append(_safe_query(value.strip()))
    canonical_query = str(scene_context.get("visual_query") or "").strip()
    if (canonical_query and is_neutral_photo_search_query(canonical_query)
            and query_preserves_photo_search_anchors(canonical_query, roles)):
        validated_queries.insert(0, _safe_query(canonical_query))
    validated_queries = list(dict.fromkeys(validated_queries))
    if validated_queries and base not in validated_queries:
        # Do not send an arbitrary caller string when a validated plan is present.
        base = validated_queries[0]
    suffix = [token for token in ("wide", "landscape") if token in _query_tokens(base)]
    subject = roles["subject"]
    action = roles["action"]
    setting = roles["setting"]
    subject_options = list(dict.fromkeys((
        subject, *_SUBJECT_SEARCH_VARIANTS.get(subject, ()),
    )))
    action_key = " ".join(_query_tokens(action))
    action_options = list(dict.fromkeys((
        action, *[value for value in _ACTION_PHRASE_ALIASES.get(action_key, ())
                  if " ".join(_query_tokens(value)) != action_key],
    )))
    setting_options = list(dict.fromkeys((
        setting, *_SETTING_SEARCH_VARIANTS.get(setting, ()),
    )))

    def compose(subject_value, action_value, setting_value):
        value = " ".join((subject_value, action_value, setting_value, *suffix))
        try:
            value = _safe_query(value)
        except SceneImageError:
            return None
        tokens = _query_tokens(value)
        return value if 4 <= len(tokens) <= 7 else None

    candidates = [base]
    candidates.extend(value for value in validated_queries if value != base)
    # Put the most useful paired synonym first: open-vocabulary archives often
    # catalogue an image under a concrete actor and place ("woman ... mirror")
    # while the planner uses a generic actor and setting ("person ... bathroom").
    # This matters because the provider budget intentionally limits each scene
    # to a few requests.
    if len(subject_options) > 1 and len(setting_options) > 1:
        candidates.append(compose(subject_options[1], action, setting_options[1]))
    if len(action_options) > 1:
        candidates.append(compose(subject, action_options[1], setting))
    if len(subject_options) > 1:
        candidates.append(compose(subject_options[1], action, setting))
    if len(setting_options) > 1:
        candidates.append(compose(subject, action, setting_options[1]))
    if (len(action_options) > 1 and len(subject_options) > 1
            and len(setting_options) > 1):
        candidates.append(compose(subject_options[1], action_options[1], setting_options[1]))
    # A second actor synonym often helps with generic people scenes while the
    # metadata gate still has to prove all three roles in one caption.
    if len(subject_options) > 2:
        candidates.append(compose(subject_options[2], action, setting))
    return list(dict.fromkeys(value for value in candidates if value))[:variant_limit]


def _matched_query_tokens(query: str, record: dict) -> set[str]:
    corpus = _metadata_tokens(record)
    return {term for term in _query_terms(query)
            if _token_forms(term) & corpus}


def _metadata_relevance(query: str, record: dict) -> float:
    terms = _query_terms(query)
    if not terms:
        return 0.0
    return len(_matched_query_tokens(query, record)) / len(terms)


def metadata_relevance(query: str, record: dict) -> float:
    """Return auditable lexical support between a neutral query and source metadata."""
    return _metadata_relevance(query, record)


# Reserved action anchor for a scene that is honestly a static/establishing
# shot (a skyline, a landmark, a product on a table) with no human action to
# verify. Must still pass normalize_photo_search_terms's own 2-4 word, non-
# overlapping-token validation like any other action anchor.
_ESTABLISHING_SHOT_ACTION = "establishing shot"


def scene_metadata_matches_query(query: str, record: dict, *, scene_context: dict | None = None) -> bool:
    """Require ordered subject, action, and setting anchors in bounded metadata.

    Search phrases use the compact order ``subject action setting``.  Requiring
    an action token between the subject and final place token prevents a loose
    topic match such as a recording studio from satisfying a request to document
    a completed task at an office. The one carve-out is the reserved action
    value ``"establishing shot"`` (see ``_ESTABLISHING_SHOT_ACTION``): a scene
    that is honestly declared as a static/establishing shot (a skyline, a
    landmark, a product on a table) has no action to verify in the first place,
    and real provider captions for that kind of photo ("Beijing skyline at
    dusk") describe subject and setting only — never an action verb. Every
    scene that declares a real action anchor still needs all three roles
    evidenced together, exactly as before.
    """
    anchors = (scene_context.get("photo_search_terms")
               if isinstance(scene_context, dict) else None)
    if anchors is not None:
        try:
            normalized = normalize_photo_search_terms(anchors)
        except ValueError:
            return False
        if not query_preserves_photo_search_anchors(query, normalized):
            return False
        is_establishing_shot = normalized["action"] == _ESTABLISHING_SHOT_ACTION
        # Do not assemble a scene from unrelated sentences in an archival
        # description. Each role must be evidenced in the same title/caption.
        for metadata_tokens in _metadata_evidence_units(record):
            if (_subject_supported(normalized["subject"], metadata_tokens)
                    and (is_establishing_shot
                         or _action_supported(normalized["action"], metadata_tokens))
                    and _setting_supported(normalized["setting"], metadata_tokens)):
                return True
        return False

    terms = _query_tokens(query)
    if len(terms) < 3:
        return False
    matched = _matched_query_tokens(query, record)
    subject_count = min(2, max(1, len(terms) - 2))
    subject_terms = terms[:subject_count]
    action_terms = terms[subject_count:-1]
    setting_term = terms[-1]
    if not any(term in matched for term in subject_terms):
        return False
    metadata_tokens = _metadata_tokens(record)
    if not (setting_term in matched or _SETTING_ALIASES.get(setting_term, set()) & metadata_tokens):
        return False
    if action_terms:
        required = max(1, math.ceil(len(action_terms) * 0.5))
        if sum(term in matched for term in action_terms) < required:
            return False
    return True


def scene_metadata_relevance(query: str, record: dict, *, scene_context: dict | None = None) -> float:
    """Return role-aware scene support, separate from broad lexical query overlap."""
    anchors = scene_context.get("photo_search_terms") if isinstance(scene_context, dict) else None
    if anchors is None:
        return _metadata_relevance(query, record)
    try:
        normalized = normalize_photo_search_terms(anchors)
    except ValueError:
        return 0.0
    if not query_preserves_photo_search_anchors(query, normalized):
        return 0.0
    best = 0
    for metadata_tokens in _metadata_evidence_units(record):
        supported = (
            _subject_supported(normalized["subject"], metadata_tokens),
            _action_supported(normalized["action"], metadata_tokens),
            _setting_supported(normalized["setting"], metadata_tokens),
        )
        best = max(best, sum(supported))
    return best / 3.0


def photo_style_matches_metadata(style: str, record: dict) -> bool:
    """Reject archive artwork when the requested visual style is photography."""
    style_text = str(style or "").casefold()
    wants_photo = any(token in style_text for token in
                      ("photo", "photograph", "摄影", "纪实", "写实"))
    if not wants_photo:
        return True
    text = f"{record.get('title') or ''} {str(record.get('description') or '')[:1200]}"
    return not bool(_PHOTO_ARTWORK_RE.search(text))


def _metadata_is_safe(record: dict) -> bool:
    """Reject obvious explicit/graphic items using provider title/description metadata."""
    text = " ".join(str(record.get(k) or "")
                    for k in ("title", "description", "attribution", "creator"))
    return not any(pattern.search(text) for pattern in _UNSAFE_METADATA_PATTERNS)


def is_safe_metadata(record: dict) -> bool:
    """Public gate helper; only checks provider-supplied descriptive metadata."""
    return _metadata_is_safe(record)


def _is_explicit_tls_handshake_timeout(exc: BaseException) -> bool:
    """Return true only when the transport explicitly identifies TLS handshake timeout.

    A generic socket timeout is deliberately not enough to trip a circuit breaker.
    """
    values = (exc, getattr(exc, "reason", None))
    for value in values:
        text = str(value or "").casefold()
        timed_out = any(token in text for token in ("timed out", "timeout", "time out"))
        if "handshake" in text and timed_out:
            return True
    return False


class _HostRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: set[str]):
        super().__init__()
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _url_is_allowed(newurl, self.allowed_hosts) or not _url_is_public_https(newurl):
            raise SceneImageError("SCENE.IMAGE_REDIRECT_BLOCKED",
                                  "图片服务尝试跳转到非公开或未允许的 HTTPS 图片域名。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _KeepAliveHTTPSHandler(urllib.request.HTTPSHandler):
    """HTTPS handler that keeps connections open and reuses them per host.

    urllib's stock handler sends ``Connection: close`` and drops the socket after
    every response, so each search and each image download paid a full TCP+TLS
    handshake — 5-10 s per request behind a slow proxy route. Here a connection
    goes back to a small per-host idle pool once its response was read to the end
    and the server did not ask to close. All callers issue idempotent GETs, so a
    reused connection the server already dropped is retried once on a fresh one.
    Proxy tunnels (CONNECT) are keyed by proxy and target host together.
    """

    _MAX_IDLE_PER_HOST = 4
    _STALE_ERRORS = (http.client.RemoteDisconnected, http.client.CannotSendRequest,
                     http.client.BadStatusLine, ConnectionResetError, BrokenPipeError)

    def __init__(self, context: ssl.SSLContext | None = None):
        super().__init__(context=context or ssl.create_default_context())
        self._idle: dict[tuple, list] = {}
        self._lock = threading.Lock()

    def _take(self, key):
        with self._lock:
            conns = self._idle.get(key) or []
            return conns.pop() if conns else None

    def _give_back(self, key, conn):
        with self._lock:
            conns = self._idle.setdefault(key, [])
            if len(conns) < self._MAX_IDLE_PER_HOST:
                conns.append(conn)
                return
        conn.close()

    def close_all(self):
        with self._lock:
            pools, self._idle = list(self._idle.values()), {}
        for conns in pools:
            for conn in conns:
                conn.close()

    def https_open(self, req):
        host = req.host
        if not host:
            raise urllib.error.URLError("no host given")
        tunnel_host = getattr(req, "_tunnel_host", None)
        key = (host, tunnel_host)
        headers = dict(req.unredirected_hdrs)
        headers.update({k: v for k, v in req.headers.items() if k not in headers})
        tunnel_headers = {}
        if "Proxy-Authorization" in headers:
            tunnel_headers["Proxy-Authorization"] = headers.pop("Proxy-Authorization")
        headers = {name.title(): val for name, val in headers.items()}

        conn = self._take(key)
        reused = conn is not None
        while True:
            if conn is None:
                conn = http.client.HTTPSConnection(host, timeout=req.timeout,
                                                   context=self._context)
                if tunnel_host:
                    conn.set_tunnel(tunnel_host, headers=tunnel_headers)
            else:
                conn.timeout = req.timeout
                if conn.sock is not None:
                    conn.sock.settimeout(req.timeout)
            try:
                conn.request(req.get_method(), req.selector, req.data, headers)
                resp = conn.getresponse()
                break
            except self._STALE_ERRORS as err:
                conn.close()
                if reused:
                    conn, reused = None, False
                    continue
                raise urllib.error.URLError(err) from None
            except OSError as err:
                conn.close()
                raise urllib.error.URLError(err) from None
            except BaseException:
                conn.close()
                raise

        handler = self
        original_close = resp.close

        def close_and_release():
            fully_read = resp.isclosed()
            original_close()
            if fully_read and not resp.will_close:
                handler._give_back(key, conn)
            else:
                conn.close()

        resp.close = close_and_release
        resp.url = req.get_full_url()
        resp.msg = resp.reason
        return resp


#: One pool per process (each job's scene build runs in its own CLI process).
_KEEPALIVE_HTTPS = _KeepAliveHTTPSHandler()

_PROXY_CHOICE_TTL_S = 5.0
_proxy_choice: tuple[float, dict] = (0.0, {})


def _live_proxy_handler() -> urllib.request.ProxyHandler:
    """Pick the HTTPS proxy per request instead of once per process.

    A desktop proxy client (Clash) can flip between "system proxy" mode, where
    it listens on a local port, and TUN mode, where that port closes and direct
    connections are intercepted instead. A proxy chosen at process start then
    makes every later request fail instantly with URLError (2026-10-28: four of
    six scenes in one run). Use the configured proxy only while its port is
    actually open; otherwise connect directly. Re-checked at most every 5 s.
    """
    global _proxy_choice
    now = time.monotonic()
    checked_at, chosen = _proxy_choice
    if now - checked_at < _PROXY_CHOICE_TTL_S:
        return urllib.request.ProxyHandler(dict(chosen))
    configured = {}
    try:
        configured = urllib.request.getproxies_environment()
        if not (configured.get("https") or configured.get("http")) and hasattr(
                urllib.request, "getproxies_macosx_sysconf"):
            configured = urllib.request.getproxies_macosx_sysconf()
    except Exception:  # noqa: BLE001 — no readable proxy config means direct
        configured = {}
    chosen = {}
    url = configured.get("https") or configured.get("http") or ""
    if url:
        parsed = urlparse(url if "://" in url else "http://" + url)
        try:
            with socket.create_connection((parsed.hostname, parsed.port or 80), timeout=0.5):
                chosen = {"https": url}
        except OSError:
            chosen = {}
    _proxy_choice = (now, chosen)
    return urllib.request.ProxyHandler(dict(chosen))


def _request_json(url: str, allowed_hosts: set[str], timeout_s: float = 18,
                  max_attempts: int = 2, headers: dict | None = None) -> dict:
    if not _url_is_allowed(url, allowed_hosts):
        raise SceneImageError("SCENE.PROVIDER_URL_INVALID", "图片检索地址不在允许域名内。")
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "WengaoPPT/1.0 (https://wengao.local; team@wengao.local) urllib/3.13",
    }
    if headers:
        request_headers.update({str(k): str(v) for k, v in headers.items()})
    req = urllib.request.Request(url, headers=request_headers)
    opener = urllib.request.build_opener(_HostRedirectHandler(allowed_hosts),
                                          _live_proxy_handler(), _KEEPALIVE_HTTPS)
    max_attempts = max(1, min(int(max_attempts), 2))
    for attempt in range(max_attempts):
        try:
            with opener.open(req, timeout=timeout_s) as response:
                body = response.read(3 * 1024 * 1024 + 1)
                if len(body) > 3 * 1024 * 1024:
                    raise SceneImageError("SCENE.PROVIDER_RESPONSE_TOO_LARGE",
                                          "图片服务搜索结果超过读取上限。")
                return json.loads(body.decode("utf-8"))
        except SceneImageError:
            raise
        except urllib.error.HTTPError as exc:
            code = ("SCENE.PROVIDER_RATE_LIMITED" if exc.code == 429 else
                    "SCENE.PROVIDER_AUTH_FAILED" if exc.code in (401, 403) else
                    "SCENE.PROVIDER_HTTP_ERROR")
            raise SceneImageError(code, f"图片检索服务返回 HTTP {exc.code}。") from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if _is_explicit_tls_handshake_timeout(exc):
                # A handshake timeout can be transient. Give this provider one
                # bounded retry before surfacing the transport error and letting
                # the per-job circuit breaker disable it.
                if attempt + 1 < max_attempts:
                    time.sleep(0.4)
                    continue
                raise SceneImageError(
                    "SCENE.PROVIDER_TLS_HANDSHAKE_TIMEOUT",
                    "图片检索服务的 HTTPS 握手重试后仍超时。",
                ) from None
            if attempt + 1 < max_attempts:
                time.sleep(0.4)
                continue
            raise SceneImageError("SCENE.PROVIDER_UNAVAILABLE",
                                  f"图片检索服务暂不可用（{type(exc).__name__}）。") from None


def _download_image(url: str, allowed_hosts: set[str], timeout_s: int = 24) -> tuple[bytes, str, tuple[int, int]]:
    if not _url_is_allowed(url, allowed_hosts):
        raise SceneImageError("SCENE.IMAGE_URL_INVALID", "图片下载地址不在允许域名内。")
    req = urllib.request.Request(url, headers={
        "Accept": "image/jpeg,image/png",
        "User-Agent": "WengaoPPT/1.0 (https://wengao.local; team@wengao.local) urllib/3.13",
    })
    opener = urllib.request.build_opener(_HostRedirectHandler(allowed_hosts),
                                          _live_proxy_handler(), _KEEPALIVE_HTTPS)
    for attempt in range(2):
        try:
            with opener.open(req, timeout=timeout_s) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > MAX_IMAGE_BYTES:
                    raise SceneImageError("SCENE.IMAGE_TOO_LARGE", "图片超过 24 MiB 限制。")
                chunks, total = [], 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_IMAGE_BYTES:
                        raise SceneImageError("SCENE.IMAGE_TOO_LARGE", "图片超过 24 MiB 限制。")
                    chunks.append(chunk)
                break
        except SceneImageError:
            raise
        except urllib.error.HTTPError as exc:
            code = "SCENE.IMAGE_RATE_LIMITED" if exc.code == 429 else "SCENE.IMAGE_FETCH_FAILED"
            raise SceneImageError(code, f"图片源返回 HTTP {exc.code}。") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            if _is_explicit_tls_handshake_timeout(exc):
                raise SceneImageError(
                    "SCENE.IMAGE_TLS_HANDSHAKE_TIMEOUT",
                    "场景图片文件的 HTTPS 握手等待超时。",
                ) from None
            if attempt == 0:
                time.sleep(0.4)
                continue
            raise SceneImageError("SCENE.IMAGE_FETCH_FAILED",
                                  f"图片下载失败（{type(exc).__name__}）。") from None
    data = b"".join(chunks)
    if not data:
        raise SceneImageError("SCENE.IMAGE_EMPTY", "图片源返回空文件。")
    if Image is None:
        raise SceneImageError("SCENE.PIL_UNAVAILABLE", "PIL/Pillow 未安装，无法处理图片格式。")
    try:
        with Image.open(io.BytesIO(data)) as image:
            fmt = str(image.format or "").upper()
            size = tuple(int(x) for x in image.size)
            image.verify()
        if fmt not in ALLOWED_FORMATS:
            raise SceneImageError("SCENE.IMAGE_FORMAT_UNSUPPORTED",
                                  "只接受可稳定嵌入的 JPEG 或 PNG 照片。")
        if size[0] < MIN_IMAGE_WIDTH or size[1] < MIN_IMAGE_HEIGHT:
            raise SceneImageError("SCENE.IMAGE_RESOLUTION_TOO_LOW",
                                  "图片分辨率不足以用作主要场景图。")
    except SceneImageError:
        raise
    except Exception as exc:
        raise SceneImageError("SCENE.IMAGE_INVALID",
                              f"下载内容无法解码为有效图片（{type(exc).__name__}）。") from None
    return data, fmt, size


def keyword_search_variants(scene_context: dict | None, fallback: str,
                            *, max_variants: int = 4) -> list[str]:
    """Short keyword phrasings for stock-photo search recall.

    Photo archives match keywords, not sentences. Measured on 2026-10-23
    against Openverse with the product's own canonical phrasing:
    ``shoppers browsing produce supermarket`` returned 0 candidates while
    ``supermarket shopping`` returned 6, and ``cashier scanning items
    checkout`` returned 0 while ``checkout cashier`` returned matches. The
    canonical sentence stays the **verification** anchor; these shorter
    strings only widen **retrieval**. Nothing here relaxes the metadata gate:
    every candidate still has to prove subject, action and setting against the
    canonical anchors, so a broader search cannot let an unrelated photo in.

    Only words already present in the scene anchors are used, so no new fact
    and no private source text is sent to the archive.
    """
    anchors = (scene_context.get("photo_search_terms")
               if isinstance(scene_context, dict) else None)
    out: list[str] = []
    if isinstance(anchors, dict):
        try:
            normalized = normalize_photo_search_terms(anchors)
        except ValueError:
            normalized = None
        if normalized:
            subject = normalized["subject"]
            action = normalized["action"]
            setting = normalized["setting"]
            action_head = " ".join(action.split()[:1])
            for value in (f"{subject} {setting}", f"{setting} {action_head}",
                          f"{subject} {action_head}", subject, setting):
                try:
                    safe = _safe_query(value)
                except SceneImageError:
                    continue
                if safe and safe not in out:
                    out.append(safe)
    if fallback:
        try:
            safe_fallback = _safe_query(fallback)
        except SceneImageError:
            safe_fallback = ""
        if safe_fallback and safe_fallback not in out:
            out.append(safe_fallback)
    return out[:max_variants]


def _bounded_scene_search(search_query, query: str, scene_context: dict,
                          *, limit: int, budget_s: float | None = None,
                          query_timeout_s: float | None = None,
                          variants: list[str] | None = None) -> list[dict]:
    """Search a few wording variants under one provider-wide time budget.

    Each API call receives the smaller of a per-query timeout and the remaining
    provider budget. Scene searches do not retry a timed-out synonym; the next
    grounded variant can still succeed, while a no-result scene cannot multiply
    the legacy timeout and retry across every query.
    """
    budget = float(budget_s or SCENE_SEARCH_PROVIDER_BUDGET_S)
    per_query = float(query_timeout_s or SCENE_SEARCH_QUERY_TIMEOUT_S)
    deadline = time.monotonic() + budget
    base_variants = scene_search_query_variants(
        query, scene_context, max_variants=SCENE_SEARCH_MAX_VARIANTS)
    if variants:
        # Short keyword cores first (they are what keyword archives can match),
        # then the planner's synonym variants so role-synonym recall is kept.
        candidates = list(dict.fromkeys([*variants, *base_variants]))
    else:
        candidates = base_variants
    out, seen, last_error = [], set(), None
    for candidate in candidates:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            rows = search_query(candidate, min(per_query, remaining))
        except SceneImageError as exc:
            last_error = exc
            if exc.code == "SCENE.PROVIDER_TLS_HANDSHAKE_TIMEOUT":
                # A synonym cannot repair a broken TLS handshake to this provider.
                break
            continue
        for row in rows:
            key = str(row.get("source_key") or "")
            if key and key not in seen:
                seen.add(key)
                out.append(row)
    if out:
        return out
    if last_error:
        raise last_error
    if time.monotonic() >= deadline:
        raise SceneImageError(
            "SCENE.SEARCH_BUDGET_EXHAUSTED",
            f"场景图检索超过单服务 {budget:g} 秒时限，未找到候选图。",
        )
    return []


class OpenversePhotoProvider:
    """Anonymous Openverse search for commercial CC0/PDM/CC BY 4.0 photos."""
    name = "openverse"
    allowed_hosts = {"api.openverse.org"}

    def search(self, query: str, limit: int = 16, *, timeout_s: float = 18,
               max_attempts: int = 2) -> list[dict]:
        params = {
            "q": _safe_query(query), "license": "cc0,pdm,by",
            "license_type": "commercial", "filter_dead": "true",
            "mature": "false", "category": "photograph",
            "size": "large", "aspect_ratio": "wide",
            "extension": "jpg,jpeg,png", "page_size": str(max(1, min(limit, 20))),
        }
        url = "https://api.openverse.org/v1/images/?" + urllib.parse.urlencode(params)
        result = _request_json(url, self.allowed_hosts, timeout_s=timeout_s,
                               max_attempts=max_attempts)
        out = []
        for item in result.get("results") or []:
            license_id = str(item.get("license") or "")
            license_version = str(item.get("license_version") or "")
            license_url = str(item.get("license_url") or "")
            media_url = str(item.get("url") or "")
            thumb = str(item.get("thumbnail") or "")
            # Openverse distinguishes the actual media file from its smaller
            # thumbnail. Prefer the original file when it is a safe public
            # HTTPS URL; retain the thumbnail as a compatibility fallback.
            download_url = media_url if _url_is_public_https(media_url) else thumb
            foreign_page = str(item.get("foreign_landing_url") or "")
            detail_page = str(item.get("detail_url") or "")
            source_page = (foreign_page if _url_is_public_https(foreign_page) else
                           detail_page if _url_is_public_https(detail_page) else "")
            out.append({
                "provider": self.name, "source_key": str(item.get("id") or ""),
                "title": _strip_html(item.get("title")),
                "description": _strip_html(item.get("description")),
                "creator": _strip_html(item.get("creator")),
                "source_page": source_page,
                "download_url": download_url,
                "license": "CC BY 4.0" if _license_key(license_id) == "by" else license_id,
                "license_version": license_version,
                "license_url": license_url,
                "attribution": _strip_html(item.get("attribution")),
                "width": int(item.get("width") or 0), "height": int(item.get("height") or 0),
            })
        return out

    def search_scene(self, query: str, *, scene_context: dict, limit: int = 16) -> list[dict]:
        return _bounded_scene_search(
            lambda candidate, timeout_s: self.search(
                candidate, limit=limit, timeout_s=timeout_s, max_attempts=2),
            query, scene_context, limit=limit,
            budget_s=SCENE_SEARCH_BUDGET_BY_PROVIDER["openverse"],
            query_timeout_s=SCENE_SEARCH_QUERY_TIMEOUT_BY_PROVIDER["openverse"],
            variants=keyword_search_variants(scene_context, query),
        )


class PexelsPhotoProvider:
    """Opt-in Pexels search; API credentials are read from process env only."""
    name = "pexels"
    allowed_hosts = {"api.pexels.com", "images.pexels.com"}
    license_url = "https://www.pexels.com/license/"

    def search(self, query: str, limit: int = 16, *, timeout_s: float = 18,
               max_attempts: int = 2) -> list[dict]:
        api_key = os.environ.get("PEXELS_API_KEY", "").strip()
        if not api_key or "\r" in api_key or "\n" in api_key:
            raise SceneImageError(
                "SCENE.PEXELS_API_KEY_MISSING",
                "Pexels 搜图需要在启动工作台的进程环境中配置 PEXELS_API_KEY；密钥不会写入任务记录。",
            )
        params = {
            "query": _safe_query(query),
            "orientation": "landscape",
            "size": "large",
            "per_page": str(max(1, min(limit, 20))),
        }
        url = "https://api.pexels.com/v1/search?" + urllib.parse.urlencode(params)
        result = _request_json(
            url, self.allowed_hosts, timeout_s=timeout_s, max_attempts=max_attempts,
            headers={"Authorization": api_key},
        )
        out = []
        for item in result.get("photos") or []:
            photo_id = str(item.get("id") or "").strip()
            photo_url = str(item.get("url") or "").strip()
            photographer = _strip_html(item.get("photographer"))
            alt = _strip_html(item.get("alt"))
            slug = ""
            try:
                slug = urllib.parse.unquote(urlparse(photo_url).path.rstrip("/").split("/")[-1])
                slug = re.sub(r"-\d+$", "", slug).replace("-", " ").replace("_", " ")
                slug = _strip_html(slug)[:240]
            except (TypeError, ValueError):
                slug = ""
            # Pexels can return an empty `alt` for a photo whose public page URL
            # still has a descriptive slug. Use only that provider metadata as
            # a bounded caption fallback; the caller still applies the original
            # subject/action/setting gate before downloading or placing it.
            caption = " ".join(dict.fromkeys(x for x in (alt, slug) if x))
            src = item.get("src") or {}
            download_url = next((str(src.get(key) or "").strip()
                                 for key in ("large2x", "large", "landscape", "original")
                                 if _url_is_allowed(str(src.get(key) or ""),
                                                    {"images.pexels.com"})), "")
            if (not photo_id or not caption or not photographer
                    or not _url_is_allowed(photo_url, {"www.pexels.com", "pexels.com"})
                    or not download_url):
                continue
            out.append({
                "provider": self.name,
                "source_key": photo_id,
                "title": alt or slug,
                "description": caption,
                "creator": photographer,
                "attribution": f"Photo by {photographer} on Pexels",
                "source_page": photo_url,
                "download_url": download_url,
                "license": "Pexels License",
                "license_version": "current",
                "license_url": self.license_url,
                "width": int(item.get("width") or 0),
                "height": int(item.get("height") or 0),
            })
        return out

    def search_scene(self, query: str, *, scene_context: dict, limit: int = 16) -> list[dict]:
        return _bounded_scene_search(
            lambda candidate, timeout_s: self.search(
                candidate, limit=limit, timeout_s=timeout_s, max_attempts=2),
            query, scene_context, limit=limit,
            budget_s=SCENE_SEARCH_BUDGET_BY_PROVIDER["pexels"],
            query_timeout_s=SCENE_SEARCH_QUERY_TIMEOUT_BY_PROVIDER["pexels"],
            variants=keyword_search_variants(scene_context, query),
        )


class PixabayPhotoProvider:
    """Opt-in Pixabay search; API credentials are read from process env only.

    Downloads and the public photo page share a single host (pixabay.com),
    unlike Pexels' separate images.pexels.com CDN, so one allowed_hosts entry
    covers both the search API and the image fetch.
    """
    name = "pixabay"
    allowed_hosts = {"pixabay.com"}
    license_url = "https://pixabay.com/service/license-summary/"

    def search(self, query: str, limit: int = 16, *, timeout_s: float = 18,
               max_attempts: int = 2) -> list[dict]:
        api_key = os.environ.get("PIXABAY_API_KEY", "").strip()
        if not api_key or "\r" in api_key or "\n" in api_key:
            raise SceneImageError(
                "SCENE.PIXABAY_API_KEY_MISSING",
                "Pixabay 搜图需要在启动工作台的进程环境中配置 PIXABAY_API_KEY；密钥不会写入任务记录。",
            )
        params = {
            "key": api_key,
            "q": _safe_query(query),
            "image_type": "photo",
            "orientation": "horizontal",
            "safesearch": "true",
            "per_page": str(max(3, min(limit, 20))),
        }
        url = "https://pixabay.com/api/?" + urllib.parse.urlencode(params)
        result = _request_json(
            url, self.allowed_hosts, timeout_s=timeout_s, max_attempts=max_attempts)
        out = []
        for item in result.get("hits") or []:
            photo_id = str(item.get("id") or "").strip()
            page_url = str(item.get("pageURL") or "").strip()
            creator = _strip_html(item.get("user"))
            # Pixabay repeats the same tag for weighting; dedupe before using
            # it as a caption, and cap it like Pexels' alt/slug fallback.
            tags = [t.strip() for t in str(item.get("tags") or "").split(",") if t.strip()]
            caption = _strip_html(", ".join(dict.fromkeys(tags)))[:240]
            download_url = next((str(item.get(key) or "").strip()
                                 for key in ("largeImageURL", "webformatURL")
                                 if _url_is_allowed(str(item.get(key) or ""),
                                                    self.allowed_hosts)), "")
            if (not photo_id or not caption or not creator
                    or not is_valid_pixabay_source_page(page_url) or not download_url):
                continue
            out.append({
                "provider": self.name,
                "source_key": photo_id,
                "title": caption,
                "description": caption,
                "creator": creator,
                "attribution": f"Photo by {creator} on Pixabay",
                "source_page": page_url,
                "download_url": download_url,
                "license": "Pixabay License",
                "license_version": None,
                "license_url": self.license_url,
                "width": int(item.get("imageWidth") or 0),
                "height": int(item.get("imageHeight") or 0),
            })
        return out

    def search_scene(self, query: str, *, scene_context: dict, limit: int = 16) -> list[dict]:
        return _bounded_scene_search(
            lambda candidate, timeout_s: self.search(
                candidate, limit=limit, timeout_s=timeout_s, max_attempts=2),
            query, scene_context, limit=limit,
            budget_s=SCENE_SEARCH_BUDGET_BY_PROVIDER["pixabay"],
            query_timeout_s=SCENE_SEARCH_QUERY_TIMEOUT_BY_PROVIDER["pixabay"],
            variants=keyword_search_variants(scene_context, query),
        )


class WikimediaCommonsPhotoProvider:
    """Commons fallback using machine-readable imageinfo license metadata."""
    name = "wikimedia-commons"
    allowed_hosts = {"commons.wikimedia.org", "upload.wikimedia.org", "thumb.wikimedia.org"}

    def _search_query(self, query_str: str, limit: int, *, timeout_s: float = 18,
                      max_attempts: int = 2) -> list[dict]:
        params = {
            "action": "query", "format": "json", "formatversion": "2",
            "generator": "search", "gsrsearch": f"{query_str} filetype:bitmap",
            "gsrnamespace": "6", "gsrlimit": str(max(1, min(limit, 20))),
            "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
            "iiurlwidth": "1600",
            "iiextmetadataversion": "latest",
            "iiextmetadatafilter": "LicenseShortName|LicenseUrl|Artist|Credit|ImageDescription|ObjectName|Attribution",
        }
        url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(params)
        result = _request_json(url, self.allowed_hosts, timeout_s=timeout_s,
                               max_attempts=max_attempts)
        pages = (result.get("query") or {}).get("pages") or []
        if isinstance(pages, dict):
            pages = list(pages.values())
        out = []
        for page in pages:
            info = (page.get("imageinfo") or [{}])[0]
            meta = info.get("extmetadata") or {}
            license_name = _strip_html((meta.get("LicenseShortName") or {}).get("value"))
            license_url = str((meta.get("LicenseUrl") or {}).get("value") or "")
            if not _is_allowed_license(license_name, license_url=license_url):
                continue
            mime = str(info.get("mime") or "").lower()
            thumb = str(info.get("thumburl") or "")
            original = str(info.get("url") or "")
            download_url = thumb if _url_is_allowed(thumb, self.allowed_hosts) else original
            if mime not in ("image/jpeg", "image/png") or not _url_is_allowed(download_url, self.allowed_hosts):
                continue
            out.append({
                "provider": self.name,
                "source_key": str(page.get("title") or ""),
                "title": _strip_html((meta.get("ObjectName") or {}).get("value")
                                     or page.get("title")),
                "creator": _strip_html((meta.get("Artist") or {}).get("value")),
                "source_page": "https://commons.wikimedia.org/wiki/" +
                              urllib.parse.quote(str(page.get("title") or "").replace(" ", "_"),
                                                   safe="/:()_"),
                "download_url": download_url,
                "license": license_name,
                "license_version": "",
                "license_url": license_url,
                "attribution": _strip_html((meta.get("Attribution") or {}).get("value")
                                            or (meta.get("Credit") or {}).get("value")),
                "description": _strip_html((meta.get("ImageDescription") or {}).get("value")),
                "width": int(info.get("width") or 0), "height": int(info.get("height") or 0),
            })
        return out

    def search(self, query: str, limit: int = 16) -> list[dict]:
        q = _safe_query(query)
        candidates_to_try = [q]
        terms = [w for w in re.findall(r"[a-z0-9]+", q.lower())
                 if w not in {"the", "and", "with", "from", "into", "while", "photo", "image", "picture",
                              "photograph", "wide", "landscape", "scene", "at", "in", "on", "of", "a", "an", "to"}
                 and len(w) >= 3]
        if len(terms) > 3:
            candidates_to_try.append(" ".join(terms[:3]))
            candidates_to_try.append(f"{terms[0]} {terms[2]}")
            candidates_to_try.append(f"{terms[2]} {terms[3]}")
            candidates_to_try.append(f"{terms[1]} {terms[2]}")
        elif len(terms) > 2:
            candidates_to_try.append(" ".join(terms[:2]))
            candidates_to_try.append(f"{terms[0]} {terms[2]}")
            candidates_to_try.append(f"{terms[1]} {terms[2]}")

        candidates_to_try = list(dict.fromkeys(candidates_to_try))
        # Collect across every bounded query variant. Returning the first
        # non-empty batch hid useful scenes whenever the exact phrase happened
        # to find unrelated but licensed archive records (for example, a
        # musician "recording" in a studio).
        last_error = None
        out = []
        seen = set()
        for candidate_q in candidates_to_try:
            try:
                res = self._search_query(candidate_q, limit)
                for row in res:
                    key = str(row.get("source_key") or "")
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    out.append(row)
            except SceneImageError as exc:
                last_error = exc
        if out:
            return out
        if last_error:
            raise last_error
        return []

    def search_scene(self, query: str, *, scene_context: dict, limit: int = 16) -> list[dict]:
        return _bounded_scene_search(
            lambda candidate, timeout_s: self._search_query(
                candidate, limit, timeout_s=timeout_s, max_attempts=2),
            query, scene_context, limit=limit,
            budget_s=SCENE_SEARCH_BUDGET_BY_PROVIDER["wikimedia-commons"],
            query_timeout_s=SCENE_SEARCH_QUERY_TIMEOUT_BY_PROVIDER["wikimedia-commons"],
            variants=keyword_search_variants(scene_context, query),
        )


SCENE_PROMPT_FIELD_ORDER = ("subject", "environment", "action_state", "lighting",
                            "composition_camera", "style", "quality")
_REVIEW_REASON_ROLE = {"SUBJECT_NOT_VISIBLE": "subject",
                       "ACTION_NOT_VISIBLE": "action",
                       "SETTING_NOT_VISIBLE": "setting"}


def scene_identity_prompt(scene: dict | None) -> str:
    """Return the plan's own frozen 7-component photo prompt, if it is safe to send.

    The visual review judges the scene's source-language identity, so generation
    has to start from that same identity. The stored plan already carries it:
    ``image_plan.positive_prompt`` when the planner supplied one, otherwise the
    seven ``image_prompt_fields``. Anything containing digits is dropped — a
    prompt that carried a metric would send source text to a remote service,
    which the neutral-English boundary forbids.
    """
    if not isinstance(scene, dict):
        return ""
    image_plan = scene.get("image_plan")
    candidate = ""
    if isinstance(image_plan, dict):
        candidate = str(image_plan.get("positive_prompt") or "").strip()
    if not candidate:
        fields = scene.get("image_prompt_fields")
        if isinstance(fields, dict):
            candidate = ", ".join(
                str(fields.get(name) or "").strip()
                for name in SCENE_PROMPT_FIELD_ORDER
                if str(fields.get(name) or "").strip())
    candidate = re.sub(r"\s+", " ", candidate).strip()
    if not candidate or len(candidate) > 300 or re.search(r"\d", candidate):
        return ""
    return candidate


def review_missing_role(diagnostics: dict | None) -> str | None:
    """Map a blocked visual-review verdict onto the role that was not confirmed."""
    if not isinstance(diagnostics, dict):
        return None
    review = diagnostics.get("visual_review")
    if not isinstance(review, dict):
        return None
    return _REVIEW_REASON_ROLE.get(str(review.get("reason_code") or ""))


class ScenePhotoProvider:
    """Search providers in order, cache verified bytes, and reject deck-wide reuse."""
    def __init__(self, cache_dir: str | Path, providers=None, *,
                 visual_reviewer=None, max_visual_reviews_per_provider: int = 4):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if providers is None:
            default_order = []
            if os.environ.get("PEXELS_API_KEY", "").strip():
                default_order.append("pexels")
            if os.environ.get("PIXABAY_API_KEY", "").strip():
                default_order.append("pixabay")
            default_order += ["wikimedia-commons", "openverse"]
            setting = os.environ.get("WENGAO_SCENE_IMAGE_PROVIDERS",
                                     ",".join(default_order))
            registry = {"openverse": OpenversePhotoProvider,
                        "wikimedia-commons": WikimediaCommonsPhotoProvider,
                        "pexels": PexelsPhotoProvider,
                        "pixabay": PixabayPhotoProvider}
            providers = [registry[x.strip()]() for x in setting.split(",")
                         if x.strip() in registry]
        self.providers = list(providers)
        self.errors = []
        # These breakers live only on this provider object, which is created for
        # one scene-asset build. A failed host in one job cannot affect later jobs.
        self.disabled_providers = set()
        self.disabled_download_hosts = set()
        self.visual_reviewer = visual_reviewer
        try:
            self.max_visual_reviews_per_provider = max(
                1, int(max_visual_reviews_per_provider))
        except (TypeError, ValueError):
            self.max_visual_reviews_per_provider = 4

    # 4 rather than 2 (2026-10-28): the local vision review is strict and a
    # provider's first two metadata matches were both judged off-scene on a
    # business deck; each review costs ~8 s on the local model.
    def fetch_one(self, query: str, *, excluded_sha256=None, excluded_source_keys=None,
                  excluded_perceptual_hashes=None, scene_context=None,
                  min_aspect: float | None = None) -> dict:
        from .scene_model import scene_review_verdict_digest
        q = _safe_query(query)
        metadata_query = q
        if isinstance(scene_context, dict) and scene_context.get("metadata_query"):
            metadata_query = _safe_query(scene_context["metadata_query"])
            anchors = scene_context.get("photo_search_terms")
            if anchors is not None and not query_preserves_photo_search_anchors(
                    metadata_query, anchors):
                raise SceneImageError("SCENE.PHOTO_SEARCH_ANCHORS_INVALID",
                                      "图片元数据校验查询没有保留原始主体、动作和地点锚点。")
        if min_aspect is not None and (not math.isfinite(float(min_aspect)) or float(min_aspect) < 1.0):
            raise SceneImageError("SCENE.ASPECT_CONSTRAINT_INVALID", "照片版位宽高比约束无效。")
        min_aspect = float(min_aspect) if min_aspect is not None else None
        provider_query = _safe_query(f"{q} wide landscape") if min_aspect else q
        excluded_sha256 = set(excluded_sha256 or ())
        excluded_source_keys = set(excluded_source_keys or ())
        excluded_perceptual_hashes = list(excluded_perceptual_hashes or ())
        if not self.providers:
            raise SceneImageError("SCENE.NO_IMAGE_PROVIDER", "没有配置可用的免费场景照片服务。")
        failures = []
        for provider in self.providers:
            if provider.name in self.disabled_providers:
                continue
            try:
                scene_search = getattr(provider, "search_scene", None)
                records = (scene_search(provider_query, scene_context=scene_context)
                           if scene_context and callable(scene_search)
                           else provider.search(provider_query))
            except SceneImageError as exc:
                row = {"provider": provider.name, **exc.as_record()}
                failures.append(row)
                # Generic timeouts stay query-scoped. An explicit TLS handshake
                # timeout, HTTP 429, or HTTP 401/403 disables only this provider
                # for this job; the loop still gives each other configured
                # provider one attempt. Auth failure (bad/expired key) is a
                # permanent fact for this job just like rate-limiting — without
                # this, every remaining scene in the deck would re-hit the same
                # provider, get the same 401, and only then fall through.
                if exc.code in ("SCENE.PROVIDER_RATE_LIMITED",
                                "SCENE.PROVIDER_TLS_HANDSHAKE_TIMEOUT",
                                "SCENE.PROVIDER_AUTH_FAILED"):
                    self.disabled_providers.add(provider.name)
                continue
            ranked = sorted(records, key=lambda record: (
                scene_metadata_relevance(metadata_query, record, scene_context=scene_context)
                if scene_context else _metadata_relevance(metadata_query, record),
                -abs(math.log(max(0.01, (int(record.get("width") or 0) /
                                         max(1, int(record.get("height") or 0))) / 1.8)))
                if int(record.get("width") or 0) and int(record.get("height") or 0) else -9.0,
                int(record.get("width") or 0) * int(record.get("height") or 0)), reverse=True)
            visual_reviews = 0
            for record in ranked:
                if not is_allowed_license(
                        record.get("license", ""),
                        version=record.get("license_version"),
                        license_url=record.get("license_url")):
                    # Recorded, not silently skipped: Wikimedia Commons is
                    # dominated by CC BY-SA, so "rows returned but none
                    # licensable" is a very different fact from "no rows" and
                    # used to be reported as the latter.
                    failures.append({"provider": provider.name,
                                     "code": "SCENE.LICENSE_NOT_ALLOWED",
                                     "detail": f"图片许可为 {record.get('license') or '未知'}，"
                                               "不在 CC0/PDM/CC BY 4.0/已授权范围内。"})
                    continue
                if not _metadata_is_safe(record):
                    failures.append({"provider": provider.name,
                                     "code": "SCENE.CONTENT_SAFETY_METADATA_BLOCK",
                                     "detail": "图片来源元数据标记了不适合普通演示文稿的露骨或图像化内容。"})
                    continue
                if scene_context and not photo_style_matches_metadata(scene_context.get("style"), record):
                    failures.append({"provider": provider.name,
                                     "code": "SCENE.PHOTO_STYLE_MISMATCH",
                                     "detail": "照片元数据标记为插画/绘画等非摄影素材，与场景要求不符。"})
                    continue
                # With a pixel-level visual reviewer downstream, captions only
                # pre-screen: 2 of the 3 anchors (subject/action/setting) in one
                # caption unit is enough to earn a review, and the reviewer makes
                # the call. Stock captions rarely name all three for abstract
                # business scenes, so the strict 3-of-3 rule discarded 60-127
                # candidates per scene before any were looked at (2026-10-29).
                # Without a reviewer the strict rule still applies.
                if scene_context and not (
                        scene_metadata_matches_query(
                            metadata_query, record, scene_context=scene_context)
                        or (self.visual_reviewer is not None
                            and scene_context.get("photo_search_terms") is not None
                            and scene_metadata_relevance(
                                metadata_query, record,
                                scene_context=scene_context) >= 2 / 3 - 1e-9)):
                    failures.append({"provider": provider.name,
                                     "code": "SCENE.IMAGE_CONTEXT_UNVERIFIED",
                                     "detail": "标题/说明没有同时支持场景主体、动作和地点锚点。"})
                    continue
                relevance = (scene_metadata_relevance(metadata_query, record, scene_context=scene_context)
                             if scene_context else _metadata_relevance(metadata_query, record))
                query_terms = _query_terms(metadata_query)
                required_terms = min(2, len(query_terms))
                matched_terms = int(round(relevance * len(query_terms)))
                if relevance < MIN_METADATA_RELEVANCE or matched_terms < required_terms:
                    failures.append({"provider": provider.name,
                                     "code": "SCENE.IMAGE_RELEVANCE_TOO_WEAK",
                                     "detail": "图片标题/说明没有足够的场景关键词依据。"})
                    continue
                source_key = f"{record.get('provider')}:{record.get('source_key')}"
                if source_key.endswith(":") or source_key in excluded_source_keys:
                    continue
                try:
                    record_aspect = (int(record.get("width") or 0) /
                                     max(1, int(record.get("height") or 0)))
                except (TypeError, ValueError):
                    record_aspect = 0.0
                if min_aspect and record_aspect and record_aspect + 0.0001 < min_aspect:
                    failures.append({"provider": provider.name,
                                     "code": "SCENE.PHOTO_ASPECT_UNSUITABLE",
                                     "detail": "检索结果方向无法满足本页双图版位；继续寻找横向场景照片。"})
                    continue
                hosts = provider.allowed_hosts
                if provider.name == "openverse":
                    download_url = str(record.get("download_url") or "")
                    parsed_media_url = urlparse(download_url)
                    media_host = (parsed_media_url.hostname or "").casefold().rstrip(".")
                    if (not _url_is_public_https(download_url)
                            or not media_host
                            or parsed_media_url.port not in (None, 443)):
                        failures.append({"provider": provider.name,
                                         "code": "SCENE.PHOTO_URL_NOT_PUBLIC_HTTPS",
                                         "detail": "Openverse 图片下载地址不是标准端口上的公网 HTTPS 媒体地址。"})
                        continue
                    # Openverse's API host is only the search host; each media
                    # file is hosted by its upstream provider. Pin this one
                    # candidate to its own normalized host and keep redirects
                    # same-host only, so accepting external media never creates
                    # a broad download-host allowlist.
                    hosts = {media_host}
                source_page = str(record.get("source_page") or "")
                if not _url_is_public_https(source_page):
                    failures.append({"provider": provider.name,
                                     "code": "SCENE.SOURCE_PAGE_NOT_PUBLIC_HTTPS",
                                     "detail": "图片来源页不是可用于署名追溯的公网 HTTPS 地址。"})
                    continue
                download_url = str(record.get("download_url") or "")
                download_host = (urlparse(download_url).hostname or "").casefold()
                if download_host and download_host in self.disabled_download_hosts:
                    continue
                try:
                    data, fmt, size = _download_image(download_url, hosts)
                except SceneImageError as exc:
                    failures.append({"provider": provider.name, **exc.as_record()})
                    if (exc.code == "SCENE.IMAGE_TLS_HANDSHAKE_TIMEOUT"
                            and download_host):
                        # A media CDN can fail independently from search and from
                        # other CDNs, so isolate only this host, not the provider.
                        self.disabled_download_hosts.add(download_host)
                        break
                    continue
                sha = hashlib.sha256(data).hexdigest()
                photo_check = photographic_asset_check(data)
                if not photo_check.get("photographic"):
                    if photo_check.get("contrast_insufficient"):
                        failures.append({
                            "provider": provider.name,
                            "code": "SCENE.PHOTO_CONTRAST_INSUFFICIENT",
                            "detail": f"下载素材对比度极低（RMS 对比度 {photo_check.get('rms_contrast')}，"
                                      f"动态范围 {photo_check.get('dynamic_range')}），缺乏可辨识细节与画面反差。"})
                    else:
                        failures.append({
                            "provider": provider.name,
                            "code": "SCENE.PHOTO_NOT_PHOTOGRAPHIC",
                            "detail": "下载素材是平涂插画/线描/矢量图而非场景照片"
                                      f"（5bit 色数 {photo_check.get('unique_5bit')}，"
                                      f"前八色占比 {photo_check.get('top8_fraction')}）。"})
                    continue
                if min_aspect and size[0] / max(1, size[1]) + 0.0001 < min_aspect:
                    failures.append({"provider": provider.name,
                                     "code": "SCENE.PHOTO_ASPECT_UNSUITABLE",
                                     "detail": "实际下载图片方向无法满足本页双图版位；继续寻找横向场景照片。"})
                    continue
                if sha in excluded_sha256:
                    continue
                phash = perceptual_hash(data)
                if any(image_hash_distance(phash, old) <= 4 for old in excluded_perceptual_hashes):
                    continue
                visual_review = None
                if self.visual_reviewer is not None:
                    if visual_reviews >= self.max_visual_reviews_per_provider:
                        failures.append({"provider": provider.name,
                                         "code": "SCENE.VISUAL_REVIEW_CANDIDATE_LIMIT",
                                         "detail": "本服务的图片视觉复核候选数达到上限；未凭元数据放行。"})
                        break
                    visual_reviews += 1
                    try:
                        visual_review = self.visual_reviewer(scene_context, data)
                    except Exception as exc:  # noqa: BLE001 — unavailable local review blocks
                        # Keep actionable model/runtime failures intact so the
                        # runner can distinguish quota, auth, and transport
                        # problems from a genuinely irrelevant photo. The
                        # production gate still blocks this candidate.
                        error_code = str(getattr(exc, "code", "") or "")
                        if error_code.startswith(("MODEL.", "SCENE.VISION_")):
                            raise
                        raise SceneImageError(
                            "SCENE.VISUAL_REVIEW_UNAVAILABLE",
                            "本机图片语义复核没有完成；该场景图被阻断。",
                        ) from None
                    review_valid = (
                        isinstance(visual_review, dict)
                        and visual_review.get("status") == "PASSED"
                        and visual_review.get("image_sha256") == sha
                        and all(visual_review.get(field) is True for field in (
                            "scene_match", "subject_visible", "action_visible",
                            "setting_visible", "photographic"))
                        and visual_review.get("confidence") == "high")
                    if not review_valid:
                        failures.append({"provider": provider.name,
                                         "code": "SCENE.VISUAL_CONTENT_MISMATCH",
                                         "detail": "本机视觉模型未能以高置信度确认照片中的主体、动作和环境；继续检查其他候选。",
                                         "visual_review": scene_review_verdict_digest(visual_review)})
                        continue
                path = self.cache_dir / f"{sha}{ALLOWED_FORMATS[fmt]}"
                if not path.exists():
                    tmp = path.with_suffix(path.suffix + ".tmp")
                    tmp.write_bytes(data)
                    os.replace(tmp, path)
                return {**record, "file": str(path), "sha256": sha,
                        "content_hash": sha,
                        "download_url": str(record.get("download_url") or ""),
                        "asset_id": str(record.get("asset_id") or record.get("id") or source_key),
                        "perceptual_hash": phash, "bytes": len(data),
                        "actual_format": fmt, "actual_width": size[0],
                        "actual_height": size[1], "query": q,
                        "metadata_relevance": round(relevance, 4),
                        "license_scope": ("Pexels License with source/creator link" if
                                           _license_key(record.get("license")) == "pexels license"
                                           else "Pixabay Content License with source/creator link" if
                                           _license_key(record.get("license")) in
                                           ("pixabay license", "pixabay content license")
                                           else "CC BY 4.0 with attribution" if
                                           _license_key(record.get("license")) in _CC_BY_4_LABELS
                                           else "CC0/PDM/Public Domain"),
                        "visual_review": visual_review,
                        "source_key": source_key}
        self.errors.extend(failures)
        if failures:
            codes = [str(row.get("code") or "") for row in failures]
            transport_rows = [row for row in failures
                              if str(row.get("code") or "") in _TRANSPORT_FAILURE_CODES]
            if transport_rows:
                # A transport failure is a service/network fact, not evidence
                # that no suitable picture exists. Keep that identity so the
                # runner and the operator do not blame the semantic model.
                # The detail must come from the transport row itself, never
                # from an unrelated eligibility rejection.
                transport_row = transport_rows[-1]
                raise SceneImageError(
                    str(transport_row.get("code")),
                    "图片检索服务未能完成请求"
                    f"（{transport_row.get('code')}）："
                    f"{transport_row.get('detail', '图片检索服务不可用。')}")
            summary = ", ".join(f"{code}×{codes.count(code)}"
                                for code in dict.fromkeys(codes))
            raise SceneImageError(
                "SCENE.PROVIDER_ROWS_INELIGIBLE",
                "图库返回了候选，但没有一张同时满足许可、格式、分辨率与去重要求；"
                f"逐项拒绝原因：{summary}")
        raise SceneImageError("SCENE.NO_LICENSED_PHOTO",
                              "图库没有返回任何与查询匹配的候选照片。")

    def generate_one(self, *, scene_context: dict, excluded_sha256=None,
                     excluded_source_keys=None, excluded_perceptual_hashes=None,
                     min_aspect: float | None = None,
                     review_focus_role: str | None = None) -> dict:
        """AI-generated fallback when no stock photo clears the licensing/metadata
        gate. Local (ComfyUI, loopback-only) by default; routes to a user-
        configured cloud image-generation service instead when one is explicitly
        enabled (wengao_ppt.cloud_model_config.active_image_gen_target()).

        Only reached after every configured stock provider has been exhausted for
        this scene (see the caller in semantic_scenes.py). The generated image is
        never trusted on the strength of the prompt alone: it goes through the
        same perceptual-dedupe and visual-review gate as a downloaded photo, so a
        generated picture that does not actually show the scene's subject,
        action, and setting is rejected exactly like a mismatched stock photo
        would be. The prompt is built from the scene's own frozen structured
        identity (see ``scene_identity_prompt``), never from the English search
        anchors alone, so what is generated and what the reviewer checks are the
        same scene. ``review_focus_role`` re-emphasizes the one role a previous
        review could not confirm on a bounded retry. With the local client, no
        bytes leave the machine. With a cloud client, the sanitized prompt is
        sent to that user-configured service — never the original-language
        source text.
        """
        from .scene_model import scene_review_verdict_digest
        from wengao_ppt.media.genimg.comfyui import ComfyUIClient, sanitize_scene_prompt
        from wengao_ppt import cloud_model_config
        from wengao_ppt.media.genimg.cloud_genimg import CloudImageClient, CloudImageGenError
        excluded_sha256 = set(excluded_sha256 or ())
        excluded_source_keys = set(excluded_source_keys or ())
        excluded_perceptual_hashes = list(excluded_perceptual_hashes or ())
        cloud_target = cloud_model_config.active_image_gen_target()
        is_cloud = cloud_target is not None
        if is_cloud and getattr(self, "_gen_permanently_broken", None) == cloud_target.get("endpoint"):
            # 本次任务里这个云端生图端点已经被判定为永久失败（坏 key/额度耗尽/
            # 请求体不被接受）：不再对后面每一个需要兜底生图的场景重复同一个
            # 注定失败的请求，直接如实失败，省下的是真实的等待时间。
            raise SceneImageError(
                "SCENE.GEN_IMAGE_PERMANENT_FAILURE",
                "云端生图服务本次任务内已判定为永久失败（认证/额度/请求参数问题），"
                "本任务内不再重试该服务。")
        client = CloudImageClient(cloud_target) if is_cloud else ComfyUIClient()
        if not client.is_available():
            if is_cloud:
                raise SceneImageError("SCENE.GEN_IMAGE_UNAVAILABLE",
                                      "云端生图服务未正确配置，无法生成替代场景图。")
            raise SceneImageError("SCENE.GEN_IMAGE_UNAVAILABLE",
                                  "本机生图服务（ComfyUI）未运行，无法生成替代场景图。")
        anchors = (scene_context or {}).get("photo_search_terms") or {
            "subject": scene_context.get("subject", ""),
            "action": scene_context.get("action", ""),
            "setting": scene_context.get("setting", ""),
        }
        scene_request = {
            "visual_query": (scene_context or {}).get("metadata_query")
                            or (scene_context or {}).get("visual_query") or "",
            "anchors": anchors,
        }
        identity_prompt = scene_identity_prompt(scene_context)
        if identity_prompt:
            scene_request["identity_prompt"] = identity_prompt
        focus_role = str(review_focus_role or "").strip().lower()
        if focus_role in _REVIEW_REASON_ROLE.values():
            focus_value = str((scene_context or {}).get(focus_role) or "").strip()
            if focus_value:
                scene_request["prompt_focus"] = focus_value
        if min_aspect:
            # Without this, the generator always returns its own default
            # aspect (square, for the cloud client) and the image is judged
            # against min_aspect only *after* generating it — so every scene
            # that needs a wide slot (a paired photo row, a wide single
            # area) was guaranteed to fail SCENE.GEN_IMAGE_ASPECT_UNSUITABLE
            # on the very first try, every time, regardless of prompt
            # quality. Request a bucket at least as wide as required —
            # only clients that understand the hint act on it; others
            # ignore the extra field exactly as before.
            if min_aspect >= 1.6:
                scene_request["target_aspect_ratio"] = "16:9"
            elif min_aspect >= 1.2:
                scene_request["target_aspect_ratio"] = "4:3"
        try:
            result = client.generate_image(scene_request, timeout_s=180.0)
        except CloudImageGenError as exc:
            if exc.permanent:
                self._gen_permanently_broken = cloud_target.get("endpoint")
                raise SceneImageError(
                    "SCENE.GEN_IMAGE_PERMANENT_FAILURE",
                    f"云端生图服务返回了不会自行恢复的错误（{exc.detail}）；"
                    "本任务内不再重试该服务。") from None
            raise SceneImageError("SCENE.GEN_IMAGE_FAILED", exc.detail) from None
        if not result or not result.get("image_bytes"):
            raise SceneImageError("SCENE.GEN_IMAGE_FAILED",
                                  "生图服务没有在预算内返回可用图片。")
        data = result["image_bytes"]
        sha = hashlib.sha256(data).hexdigest()
        if sha in excluded_sha256:
            raise SceneImageError("SCENE.GEN_IMAGE_DUPLICATE", "生成的图片与本稿已用图片重复。")
        photo_check = photographic_asset_check(data)
        if not photo_check.get("photographic"):
            raise SceneImageError("SCENE.GEN_IMAGE_NOT_PHOTOGRAPHIC",
                                  "生成的图片不是可辨识的写实场景画面，已拒绝使用。")
        if Image is None:
            raise SceneImageError("SCENE.PIL_UNAVAILABLE", "PIL/Pillow 未安装，无法处理生成的图片。")
        try:
            with Image.open(io.BytesIO(data)) as im:
                fmt = str(im.format or "").upper()
                size = tuple(int(x) for x in im.size)
                im.verify()
        except Exception:
            raise SceneImageError("SCENE.GEN_IMAGE_DECODE_FAILED", "生成的图片无法解码。") from None
        if fmt not in ALLOWED_FORMATS:
            raise SceneImageError("SCENE.GEN_IMAGE_FORMAT_UNSUPPORTED",
                                  "生图服务返回的格式不是可嵌入的 JPEG/PNG。")
        if size[0] < MIN_IMAGE_WIDTH or size[1] < MIN_IMAGE_HEIGHT:
            raise SceneImageError("SCENE.GEN_IMAGE_RESOLUTION_TOO_LOW",
                                  "生成图片分辨率不足以用作主要场景图。")
        if min_aspect and size[0] / max(1, size[1]) + 0.0001 < min_aspect:
            raise SceneImageError("SCENE.GEN_IMAGE_ASPECT_UNSUITABLE",
                                  "生成图片方向无法满足本页版位。")
        phash = perceptual_hash(data)
        if any(image_hash_distance(phash, old) <= 4 for old in excluded_perceptual_hashes):
            raise SceneImageError("SCENE.GEN_IMAGE_NEAR_DUPLICATE", "生成的图片与本稿已有图片过于相似。")
        source_prefix = "cloud_genimg" if is_cloud else "comfyui"
        source_key = f"{source_prefix}:{result.get('prompt_digest', '')}:{result.get('seed', '')}"
        if source_key in excluded_source_keys:
            raise SceneImageError("SCENE.GEN_IMAGE_DUPLICATE", "生成的图片来源标识与本稿已用图片重复。")
        visual_review = None
        if self.visual_reviewer is not None:
            try:
                visual_review = self.visual_reviewer(scene_context, data)
            except Exception as exc:  # noqa: BLE001 — see fetch_one's identical handling
                error_code = str(getattr(exc, "code", "") or "")
                if error_code.startswith(("MODEL.", "SCENE.VISION_")):
                    raise
                raise SceneImageError(
                    "SCENE.VISUAL_REVIEW_UNAVAILABLE",
                    "图片语义复核没有完成；生成的场景图被阻断。") from None
            review_valid = (
                isinstance(visual_review, dict)
                and visual_review.get("status") == "PASSED"
                and visual_review.get("image_sha256") == sha
                and all(visual_review.get(field) is True for field in (
                    "scene_match", "subject_visible", "action_visible",
                    "setting_visible", "photographic"))
                and visual_review.get("confidence") == "high")
            if not review_valid:
                raise SceneImageError(
                    "SCENE.VISUAL_CONTENT_MISMATCH",
                    "视觉模型未能以高置信度确认生成图片中的主体、动作和环境。",
                    diagnostics={"visual_review":
                                 scene_review_verdict_digest(visual_review)})
        path = self.cache_dir / f"{sha}{ALLOWED_FORMATS[fmt]}"
        if not path.exists():
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
        prompt_text = sanitize_scene_prompt(scene_request)
        if is_cloud:
            license_scope = "云端生图（用户自配服务，按该服务条款，非真实照片）"
            license_name = "AI_GENERATED_CLOUD"
            attribution = (f"云端生图（{result.get('generator', 'cloud')}, "
                           f"{result.get('model', 'default')}），提示词：{prompt_text}")
            title = "云端生成场景图"
            provider = "cloud_image_generation"
        else:
            license_scope = "本机生成（无第三方版权，非真实照片）"
            license_name = "AI_GENERATED_LOCAL"
            attribution = f"本机生成（ComfyUI, {result.get('model', 'default')}），提示词：{prompt_text}"
            title = "本机生成场景图"
            provider = "comfyui_local_generation"
        return {
            "file": str(path), "sha256": sha, "content_hash": sha,
            "download_url": "", "asset_id": source_key,
            "perceptual_hash": phash, "bytes": len(data),
            "actual_format": fmt, "actual_width": size[0], "actual_height": size[1],
            "query": scene_request["visual_query"], "metadata_relevance": 1.0,
            "license_scope": license_scope,
            "license": license_name, "license_version": None, "license_url": None,
            "attribution": attribution,
            "title": title, "description": prompt_text,
            "creator": None, "source_page": "",
            "visual_review": visual_review, "source_key": source_key,
            "provider": provider,
        }


# ----------------------------------------------------------------- Provider-Aware Prompt Architecture

DEFAULT_QUALITY_PREFERENCES = [
    "高清",
    "细节丰富",
    "真实质感",
    "专业摄影",
]

DEFAULT_NEGATIVE_PROMPT_TERMS = [
    "低质量",
    "模糊",
    "畸形手指",
    "多余肢体",
    "文字乱码",
    "水印",
    "过曝",
]

DEFAULT_NEGATIVE_PROMPT_EN = (
    "low quality, blurry, deformed fingers, extra limbs, garbled text, watermark, overexposed, distorted"
)

PROVIDER_REGISTRY = {
    "wikimedia-commons": {
        "mode": "photo_search",
        "supports_negative_prompt": False,
        "supports_t2i": False,
        "max_query_words": 4,
    },
    "openverse": {
        "mode": "photo_search",
        "supports_negative_prompt": False,
        "supports_t2i": False,
        "max_query_words": 4,
    },
    "pexels": {
        "mode": "photo_search",
        "supports_negative_prompt": False,
        "supports_t2i": False,
        "max_query_words": 4,
    },
    "stable_diffusion": {
        "mode": "text_to_image",
        "supports_negative_prompt": True,
        "supports_t2i": True,
    },
    "sdxl": {
        "mode": "text_to_image",
        "supports_negative_prompt": True,
        "supports_t2i": True,
    },
    "dall_e": {
        "mode": "text_to_image",
        "supports_negative_prompt": False,
        "supports_t2i": True,
    },
}


def resolve_scene_lighting(environment: str, action_state: str = "",
                           preferred_lighting: str | None = None) -> str:
    """Resolve scene-accurate lighting, ensuring 'natural daylight' NEVER overwrites
    night, industrial, hospital, underground, or server room lighting.
    """
    ctx = f"{environment} {action_state}".lower()

    # 1. Server room / data center / gaming / cyber LED
    if (re.search(r"\b(server|data center|server room|datacenter|esports|rgb|led)\b", ctx) or
            re.search(r"(?:机房|服务器|电竞|指示灯)", ctx)):
        return "cool blue LED ambient server room lighting"

    # 2. Night / evening / dusk / moonlight
    if (re.search(r"\b(night|evening|dusk|midnight|sunset|twilight|darkness)\b", ctx) or
            re.search(r"(?:夜晚|深夜|晚上|黄昏|傍晚|落日|月光|夜市)", ctx)):
        return "atmospheric evening night illumination with streetlights"

    # 3. Hospital / clinic / cleanroom / medical
    if (re.search(r"\b(hospital|clinic|radiolog\w*|ward|surgery|laboratory|operating room|cleanroom|icu|mri|ct scan)\b", ctx) or
            re.search(r"(?:医院|门诊|病房|诊室|放射科|洁净车间|手术室|无菌)", ctx)):
        return "sterile clean diffused hospital interior lighting"

    # 4. Industrial workshop / welding / factory / mining
    if (re.search(r"\b(factory|assembly line|production line|machining|welding|metal cut\w*|foundry|mining|workshop|quarry)\b", ctx) or
            re.search(r"(?:车间|工厂|流水线|焊接|机床|冲压|装配线|采矿|矿场|钢厂)", ctx)):
        if "weld" in ctx or "spark" in ctx or "焊接" in ctx:
            return "industrial workshop lighting with welding sparks"
        return "high-bay overhead industrial factory lighting"

    # 5. Morning / dawn / sunrise
    if (re.search(r"\b(morning|sunrise|dawn)\b", ctx) or
            re.search(r"(?:清晨|早晨|晨光|日出|朝阳)", ctx)):
        return "soft warm morning sunrise light"

    # 6. Reading desk / office / library
    if (re.search(r"\b(desk|office|library|study|lamp)\b", ctx) or
            re.search(r"(?:书桌|办公室|台灯|阅读|自习|图书馆)", ctx)):
        return "warm focused desk lamp lighting"

    # 7. Outdoors / park / field / ocean / sky / rooftop
    if (re.search(r"\b(park|field|mountain|nature|farm|trail|hike|wheat|outdoor|ocean|sea|beach|marine|lake|river|sky|rooftop|suburb\w*|courtyard)\b", ctx) or
            re.search(r"(?:公园|麦田|农田|山路|步道|户外|海洋|海面|大海|天空|湖泊|河流|屋顶|天台|庭院|郊区)", ctx)):
        return "natural outdoor sunlight"

    # 8. User preference or default fallback
    if preferred_lighting and preferred_lighting.strip():
        pref = preferred_lighting.strip()
        has_conflict = (
            re.search(r"\b(night|factory|hospital|server)\b", ctx) or
            re.search(r"(?:机房|夜晚|深夜|车间|工厂|医院)", ctx)
        )
        if not has_conflict:
            if "自然光" in pref:
                return "natural outdoor sunlight"
            return pref
    return "balanced ambient room lighting"


def build_provider_search_query(subject: str, environment: str = "",
                                action_state: str = "", *, max_words: int = 4) -> str:
    """Generate an ordered subject + action + setting English search phrase.

    Never contains SD negative tokens, 8K buzzwords, URLs, or long clauses.
    """
    stop_words = {
        "the", "a", "an", "and", "or", "with", "in", "at", "on", "of", "for", "to",
        "by", "is", "are", "while", "during", "from", "into", "shown", "showing",
        "visible", "photo", "image", "picture", "photograph", "hd", "8k", "4k",
        "realistic", "quality", "clean", "beautiful", "high", "best",
    }
    def field_tokens(value: str) -> list[str]:
        raw = re.sub(r"[^a-z0-9\s]+", " ", str(value or "").lower())
        return list(dict.fromkeys(t for t in raw.split()
                                  if len(t) >= 3 and t not in stop_words))

    subject_terms = field_tokens(subject)
    action_terms = field_tokens(action_state)
    setting_terms = field_tokens(environment)
    maximum = max(3, min(7, int(max_words or 4)))
    # Keep all validated anchors in role order. For legacy callers with longer
    # fields, trim only excess action descriptors while preserving an object
    # word and the final setting anchor.
    subject_terms = subject_terms[-2:]
    action_terms = action_terms[:4]
    setting_terms = setting_terms[:2]
    if len(subject_terms) + len(action_terms) + len(setting_terms) > maximum:
        subject_terms = subject_terms[-1:]
        setting_terms = setting_terms[:1]
        action_budget = maximum - len(subject_terms) - len(setting_terms)
        action_terms = action_terms[:action_budget]
    if not subject_terms or len(action_terms) < 2 or not setting_terms:
        selected = []
    else:
        selected = subject_terms + action_terms + setting_terms
        if len(selected) > maximum:
            selected = []
    selected = list(dict.fromkeys(selected))
    if not selected:
        # Diagnostic-only fallback for callers with non-English fields and no
        # structured English search anchors. It cannot authorize an image.
        selected = ["scene", "photo"]
    candidate = " ".join(selected)
    return candidate if len(candidate) <= MAX_QUERY_CHARS else candidate[:MAX_QUERY_CHARS]


def build_positive_prompt(subject: str, environment: str, action_state: str,
                          lighting: str, composition_camera: str, style: str,
                          quality: str) -> str:
    """Assemble 7-component structured text-to-image prompt."""
    components = [
        str(subject or "").strip(),
        str(environment or "").strip(),
        str(action_state or "").strip(),
        str(lighting or "").strip(),
        str(composition_camera or "").strip(),
        str(style or "").strip(),
        str(quality or "").strip(),
    ]
    valid = [c for c in components if c]
    return ", ".join(valid)


def adapt_prompt_for_provider(plan: dict, provider_name: str) -> dict:
    """Adapt structured prompt plan to target provider capabilities.

    - Photo search providers: provider_query only, negative_prompt is None, no SD tokens.
    - SD/T2I providers: positive_prompt + negative_prompt (if supported).
    """
    provider_info = PROVIDER_REGISTRY.get(provider_name.lower(), {
        "mode": "photo_search" if provider_name in ("wikimedia-commons", "openverse", "pexels") else "text_to_image",
        "supports_negative_prompt": provider_name in ("stable_diffusion", "sdxl"),
        "supports_t2i": provider_name in ("stable_diffusion", "sdxl", "dall_e"),
    })

    mode = provider_info.get("mode", "photo_search")
    supports_neg = provider_info.get("supports_negative_prompt", False)

    adapted = dict(plan)
    adapted["provider"] = provider_name
    adapted["provider_mode"] = mode

    # Ensure lighting does not conflict with night/industrial/hospital
    resolved_lighting = resolve_scene_lighting(
        environment=plan.get("environment", ""),
        action_state=plan.get("action_state", ""),
        preferred_lighting=plan.get("lighting"),
    )
    adapted["lighting"] = resolved_lighting

    # Positive prompt
    adapted["positive_prompt"] = build_positive_prompt(
        subject=plan.get("subject", ""),
        environment=plan.get("environment", ""),
        action_state=plan.get("action_state", ""),
        lighting=resolved_lighting,
        composition_camera=plan.get("composition_camera", "eye-level medium shot"),
        style=plan.get("style", "documentary photography"),
        quality=plan.get("quality", "sharp focus, high resolution"),
    )

    # Provider query for search
    query = plan.get("provider_query")
    if not query or mode == "photo_search":
        query = build_provider_search_query(
            subject=plan.get("subject", ""),
            environment=plan.get("environment", ""),
            action_state=plan.get("action_state", ""),
            max_words=provider_info.get("max_query_words", 4),
        )
    adapted["provider_query"] = query

    # Negative prompt handling: ONLY when provider explicitly supports it
    if supports_neg:
        adapted["negative_prompt"] = plan.get("negative_prompt") or DEFAULT_NEGATIVE_PROMPT_EN
    else:
        adapted["negative_prompt"] = None

    return adapted


def build_image_plan(
    *,
    source_ref: str,
    source_language: str,
    canonical_scene_id: str,
    scene_family: str,
    subject: str,
    environment: str,
    action_state: str,
    lighting: str | None = None,
    composition_camera: str | None = None,
    style: str | None = None,
    quality: str | None = None,
    provider_name: str = "wikimedia-commons",
    asset_url: str | None = None,
    license_name: str = "Public domain",
    asset_sha256: str | None = None,
    slide_index: int = 1,
    placement: dict | None = None,
    dedupe_decision: str = "unique_placement",
    review_status: str = "PENDING_VERIFICATION",
) -> dict:
    """Build complete 22-field image plan adhering to hard-gate.md specification."""
    resolved_lighting = resolve_scene_lighting(environment, action_state, lighting)
    comp = composition_camera or "eye-level medium shot"
    st = style or "documentary realism"
    ql = quality or "sharp focus, detailed textures"

    search_query = build_provider_search_query(subject, environment, action_state)

    raw_plan = {
        "source_ref": str(source_ref),
        "source_language": str(source_language),
        "canonical_scene_id": str(canonical_scene_id),
        "scene_family": str(scene_family),
        "subject": str(subject),
        "environment": str(environment),
        "action_state": str(action_state),
        "lighting": resolved_lighting,
        "composition_camera": comp,
        "style": st,
        "quality": ql,
        "positive_prompt": build_positive_prompt(subject, environment, action_state, resolved_lighting, comp, st, ql),
        "negative_prompt": None,
        "provider_mode": "photo_search",
        "provider_query": search_query,
        "asset_url": asset_url,
        "license": license_name,
        "asset_sha256": asset_sha256,
        "slide_index": int(slide_index),
        "placement": placement or {"fit_mode": "contain", "safe_area": True, "zero_crop": True},
        "dedupe_decision": str(dedupe_decision),
        "review_status": str(review_status),
    }

    return adapt_prompt_for_provider(raw_plan, provider_name)

