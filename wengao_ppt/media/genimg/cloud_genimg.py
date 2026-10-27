"""wengao_ppt.media.genimg.cloud_genimg: optional cloud image-generation client.

Only used when the user has explicitly enabled and configured cloud image
generation (wengao_ppt.cloud_model_config.active_image_gen_target() returns
non-None). Mirrors wengao_ppt.media.genimg.comfyui.ComfyUIClient's public
interface (is_available / generate_image) so the caller in scene_images.py
can pick either client without caring which one it got.

Discipline, same as the local ComfyUI client:
- Prompt sanitization: only scenes/objects, no real persons, brands, or
  trademarked locations (reuses sanitize_scene_prompt from comfyui.py).
- Complete metadata registration: generator, model, prompt_digest.
- No credentials or raw prompt bytes are ever logged; the caller is
  responsible for not writing this module's return value into shared job
  records beyond the already-established fields.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from .comfyui import sanitize_scene_prompt


class CloudImageClient:
    """HTTP client for a user-configured cloud image-generation endpoint."""

    def __init__(self, target: Dict[str, str]):
        self.endpoint = target["endpoint"].rstrip("/")
        self.api_key = target["api_key"]
        self.model = target["model"]
        self.provider = target.get("provider") or "openai-images"

    def is_available(self, timeout_s: float = 2.0) -> bool:
        # Configuration itself (enabled + endpoint + key + model) is the
        # availability signal here; there is no cheap unauthenticated probe
        # endpoint common to both provider shapes, and a real probe would
        # cost a paid generation call. Transport/auth failures surface on
        # the actual generate_image() call instead.
        return bool(self.endpoint and self.api_key and self.model)

    def generate_image(
        self,
        scene_request: Dict[str, Any],
        timeout_s: float = 60.0,
        **_ignored: Any,
    ) -> Optional[Dict[str, Any]]:
        """Generate one image from scene_request; same contract as
        ComfyUIClient.generate_image (unused kwargs like checkpoint_name/seed
        are accepted and ignored for interface compatibility)."""
        prompt_text = sanitize_scene_prompt(scene_request)
        aspect = scene_request.get("target_aspect_ratio")
        try:
            if self.provider == "gemini-native":
                image_bytes = self._generate_gemini(prompt_text, timeout_s, aspect)
            else:
                image_bytes = self._generate_openai_images(prompt_text, timeout_s, aspect)
        except Exception:
            return None
        if not image_bytes:
            return None
        return {
            "image_bytes": image_bytes,
            "generator": "cloud:" + self.provider,
            "model": self.model,
            "seed": None,
            "prompt_digest": prompt_text[:160],
        }

    def _generate_gemini(self, prompt_text: str, timeout_s: float,
                        aspect: str | None = None) -> Optional[bytes]:
        generation_config: Dict[str, Any] = {"responseModalities": ["IMAGE"]}
        if aspect:
            # Without this, Gemini image models return their own default
            # (square) regardless of what the slide slot actually needs,
            # and the caller only finds out after generating — guaranteeing
            # rejection for every wide slot on the very first attempt.
            generation_config["imageConfig"] = {"aspectRatio": aspect}
        payload = {
            "contents": [{"parts": [{"text": prompt_text}]}],
            "generationConfig": generation_config,
        }
        url = f"{self.endpoint}/models/{self.model}:generateContent?key={self.api_key}"
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            resp = json.loads(r.read().decode("utf-8"))
        candidates = resp.get("candidates") or []
        if not candidates:
            return None
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        for part in parts:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                return base64.b64decode(inline["data"])
        return None

    def _generate_openai_images(self, prompt_text: str, timeout_s: float,
                                aspect: str | None = None) -> Optional[bytes]:
        payload = {"model": self.model, "prompt": prompt_text, "n": 1,
                   "response_format": "b64_json"}
        if aspect == "16:9":
            payload["size"] = "1792x1024"
        elif aspect == "4:3":
            payload["size"] = "1024x1024"
        req = urllib.request.Request(
            self.endpoint + "/images/generations",
            data=json.dumps(payload).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + self.api_key)
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            resp = json.loads(r.read().decode("utf-8"))
        rows = resp.get("data") or []
        if not rows or not rows[0].get("b64_json"):
            return None
        return base64.b64decode(rows[0]["b64_json"])
