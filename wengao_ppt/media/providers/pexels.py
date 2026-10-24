"""wengao_ppt.media.providers.pexels: Pexels API provider with rate limiting.

Adheres to:
- Free tier rate limits: 200 requests/hour, 20,000 requests/month.
- API key read strictly from PEXELS_API_KEY environment variable.
- Full attribution metadata (photographer, Pexels source page, license).
- Support for orientation (landscape, portrait, square).
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from wengao_ppt.scene_images import (
    SceneImageError,
    _bounded_scene_search,
    _download_image,
    _request_json,
    _safe_query,
    _strip_html,
    _url_is_allowed,
    keyword_search_variants,
    photographic_asset_check,
)

HOURLY_LIMIT = 200
MONTHLY_LIMIT = 20000
RATE_LIMIT_FILE = Path(os.environ.get("WENGAO_RATE_LIMIT_DIR", Path.home() / ".cache" / "wengao_ppt")) / "pexels_rate_limit.json"


class PexelsRateLimiter:
    """Persistent local rate limiter for Pexels API."""

    def __init__(self, limit_file: Optional[Path] = None):
        self.file_path = limit_file or RATE_LIMIT_FILE

    def _read(self) -> Dict[str, Any]:
        if not self.file_path.is_file():
            return {
                "hourly_start": time.time(),
                "hourly_count": 0,
                "monthly_start": time.time(),
                "monthly_count": 0,
            }
        try:
            return json.loads(self.file_path.read_text(encoding="utf-8"))
        except Exception:
            return {
                "hourly_start": time.time(),
                "hourly_count": 0,
                "monthly_start": time.time(),
                "monthly_count": 0,
            }

    def _write(self, data: Dict[str, Any]) -> None:
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            self.file_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def check_and_increment(self) -> bool:
        """Check if request is within quota and record usage.

        Returns:
            True if allowed, False if rate limited.
        """
        now = time.time()
        data = self._read()

        # Check hourly window
        if now - data.get("hourly_start", 0) > 3600:
            data["hourly_start"] = now
            data["hourly_count"] = 0

        # Check monthly window (30 days)
        if now - data.get("monthly_start", 0) > 30 * 86400:
            data["monthly_start"] = now
            data["monthly_count"] = 0

        if data.get("hourly_count", 0) >= HOURLY_LIMIT:
            return False
        if data.get("monthly_count", 0) >= MONTHLY_LIMIT:
            return False

        data["hourly_count"] = data.get("hourly_count", 0) + 1
        data["monthly_count"] = data.get("monthly_count", 0) + 1
        self._write(data)
        return True

    def get_status(self) -> Dict[str, Any]:
        data = self._read()
        return {
            "hourly_count": data.get("hourly_count", 0),
            "hourly_limit": HOURLY_LIMIT,
            "monthly_count": data.get("monthly_count", 0),
            "monthly_limit": MONTHLY_LIMIT,
        }


class PexelsProvider:
    """Pexels image provider with orientation and rate limit protection."""

    name = "pexels"
    allowed_hosts = {"api.pexels.com", "images.pexels.com"}
    license_url = "https://www.pexels.com/license/"

    def __init__(self, rate_limiter: Optional[PexelsRateLimiter] = None):
        self.rate_limiter = rate_limiter or PexelsRateLimiter()

    def search(
        self,
        query: str,
        limit: int = 16,
        orientation: str = "landscape",
        *,
        timeout_s: float = 18,
        max_attempts: int = 2,
    ) -> List[Dict[str, Any]]:
        api_key = os.environ.get("PEXELS_API_KEY", "").strip()
        if not api_key:
            return []

        if not self.rate_limiter.check_and_increment():
            # Rate limit exceeded; silently return empty so fallback chain activates
            return []

        norm_orientation = orientation.lower()
        if norm_orientation not in ("landscape", "portrait", "square"):
            norm_orientation = "landscape"

        params = {
            "query": _safe_query(query),
            "orientation": norm_orientation,
            "size": "large",
            "per_page": str(max(1, min(limit, 20))),
        }
        url = "https://api.pexels.com/v1/search?" + urllib.parse.urlencode(params)
        try:
            result = _request_json(
                url,
                self.allowed_hosts,
                timeout_s=timeout_s,
                max_attempts=max_attempts,
                headers={"Authorization": api_key},
            )
        except Exception:
            return []

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
            except Exception:
                slug = ""

            caption = " ".join(dict.fromkeys(x for x in (alt, slug) if x))
            src = item.get("src") or {}
            download_url = next(
                (
                    str(src.get(key) or "").strip()
                    for key in ("large2x", "large", "landscape", "original")
                    if _url_is_allowed(str(src.get(key) or ""), {"images.pexels.com"})
                ),
                "",
            )

            if not photo_id or not photographer or not download_url:
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
