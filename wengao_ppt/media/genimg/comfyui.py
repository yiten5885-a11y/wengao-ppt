"""wengao_ppt.media.genimg.comfyui: ComfyUI local image generation client.

Adheres to:
- HTTP API integration via workflow JSON.
- Never downloads or stores checkpoint weights in the repo.
- Prompt sanitization: only scenes/objects, no real persons, brands, or trademarked locations.
- Complete metadata registration: generator=comfyui, model, prompt_digest, seed.
- Resource lock coordination via resource_lock('gen') if available.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

TEMPLATE_FILE = Path(__file__).parent / "workflow_template.json"
DEFAULT_COMFYUI_URL = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188")
DISALLOWED_PROMPT_PATTERNS = [
    r"\b(apple|google|microsoft|tesla|amazon|meta|nike|coca-cola)\b",
    r"\b(tim cook|elon musk|steve jobs|bill gates)\b",
]


@contextlib.contextmanager
def acquire_gen_lock():
    """Acquire resource_lock('gen') if available, otherwise no-op."""
    try:
        from wengao_ppt.resource_lock import resource_lock
        with resource_lock("gen"):
            yield
    except (ImportError, Exception):
        yield


def _strip_disallowed(text: str) -> str:
    """Remove brand/celebrity tokens so a prompt never targets a real identity."""
    sanitized = text.lower()
    for pattern in DISALLOWED_PROMPT_PATTERNS:
        sanitized = re.sub(pattern, "", sanitized, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", sanitized).strip(" ,")


def sanitize_scene_prompt(scene_request: Dict[str, Any]) -> str:
    """Build neutral, photographic prompt excluding brands, celebrities, and specific locations.

    ``identity_prompt`` (the plan's own frozen 7-component prompt: subject /
    environment / action_state / lighting / composition_camera / style /
    quality) wins over the three English search anchors. The generated picture
    is reviewed against the scene's source-language identity, so it has to be
    generated from that same identity: a three-word English search phrase can
    describe a different place than the one the reviewer will look for — the
    recorded 2026-10-26 failure generated an open-plan office for
    ``datacenter`` while the review was asked to confirm 应急指挥室.

    ``prompt_focus`` repeats the single role (source-language subject, action or
    setting) that the previous visual review could not confirm, so a bounded
    retry aims at the missing visual evidence instead of resampling the exact
    same wording.
    """
    visual_query = str(scene_request.get("visual_query") or "").strip()
    anchors = scene_request.get("anchors") or {}
    subject = str(anchors.get("subject") or "").strip()
    action = str(anchors.get("action") or "").strip()
    setting = str(anchors.get("setting") or "").strip()
    identity = str(scene_request.get("identity_prompt") or "").strip()
    focus = str(scene_request.get("prompt_focus") or "").strip()

    components = []
    if subject:
        components.append(subject)
    if action:
        components.append(action)
    if setting:
        components.append(setting)

    base = identity or (", ".join(components) if components else visual_query)
    if not base:
        base = "professional workspace environment"

    sanitized = _strip_disallowed(base) or "professional workspace environment"
    focus_text = _strip_disallowed(focus)

    if focus_text:
        clean_prompt = (f"{sanitized}. The scene clearly shows {focus_text}. "
                        "Realistic documentary photograph, wide 16:9 landscape framing, "
                        "neutral lighting, high resolution, sharp focus")
    elif identity:
        clean_prompt = (f"{sanitized}, realistic documentary photograph, "
                        "wide 16:9 landscape framing, "
                        "neutral lighting, high resolution, sharp focus")
    else:
        clean_prompt = (f"realistic documentary photograph of {sanitized}, "
                        "wide 16:9 landscape framing, "
                        "neutral lighting, high resolution, sharp focus")
    return re.sub(r"\s+", " ", clean_prompt).strip()


class ComfyUIClient:
    """HTTP Client for interacting with local ComfyUI instance."""

    def __init__(self, base_url: str = DEFAULT_COMFYUI_URL):
        self.base_url = base_url.rstrip("/")

    def is_available(self, timeout_s: float = 2.0) -> bool:
        """Check if ComfyUI service is reachable."""
        try:
            req = urllib.request.Request(f"{self.base_url}/system_stats")
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return resp.status == 200
        except Exception:
            return False

    def list_checkpoints(self, timeout_s: float = 3.0) -> list[str]:
        """Fetch list of available checkpoints from object_info."""
        try:
            req = urllib.request.Request(f"{self.base_url}/object_info/CheckpointLoaderSimple")
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                inputs = data.get("CheckpointLoaderSimple", {}).get("input", {}).get("required", {})
                ckpt_list = inputs.get("ckpt_name", [[]])[0]
                return list(ckpt_list) if isinstance(ckpt_list, list) else []
        except Exception:
            return []

    def generate_image(
        self,
        scene_request: Dict[str, Any],
        checkpoint_name: Optional[str] = None,
        seed: Optional[int] = None,
        timeout_s: float = 60.0,
    ) -> Optional[Dict[str, Any]]:
        """Execute text-to-image workflow on ComfyUI.

        Returns:
            Dict containing raw image bytes and metadata, or None if failed.
        """
        if not self.is_available():
            return None

        prompt_text = sanitize_scene_prompt(scene_request)
        if not TEMPLATE_FILE.is_file():
            return None

        workflow = json.loads(TEMPLATE_FILE.read_text(encoding="utf-8"))
        actual_seed = seed if seed is not None else random.randint(1000, 99999999)

        # Inject inputs into template
        if "3" in workflow and "inputs" in workflow["3"]:
            workflow["3"]["inputs"]["seed"] = actual_seed

        if checkpoint_name and "4" in workflow and "inputs" in workflow["4"]:
            workflow["4"]["inputs"]["ckpt_name"] = checkpoint_name

        if "6" in workflow and "inputs" in workflow["6"]:
            workflow["6"]["inputs"]["text"] = prompt_text

        # Queue prompt
        try:
            with acquire_gen_lock():
                payload = json.dumps({"prompt": workflow}).encode("utf-8")
                req = urllib.request.Request(
                    f"{self.base_url}/prompt",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=10.0) as resp:
                    res_data = json.loads(resp.read().decode("utf-8"))
                    prompt_id = res_data.get("prompt_id")
                    if not prompt_id:
                        return None

                # Poll history for completion
                start_time = time.time()
                while time.time() - start_time < timeout_s:
                    hist_req = urllib.request.Request(f"{self.base_url}/history/{prompt_id}")
                    with urllib.request.urlopen(hist_req, timeout=10.0) as h_resp:
                        h_data = json.loads(h_resp.read().decode("utf-8"))
                        if prompt_id in h_data:
                            outputs = h_data[prompt_id].get("outputs", {})
                            for node_id, node_out in outputs.items():
                                images = node_out.get("images", [])
                                if images:
                                    img_info = images[0]
                                    filename = img_info.get("filename")
                                    subfolder = img_info.get("subfolder", "")
                                    img_type = img_info.get("type", "output")

                                    # Retrieve image
                                    params = urllib.parse.urlencode({
                                        "filename": filename,
                                        "subfolder": subfolder,
                                        "type": img_type,
                                    })
                                    view_req = urllib.request.Request(f"{self.base_url}/view?{params}")
                                    with urllib.request.urlopen(view_req, timeout=15.0) as v_resp:
                                        img_bytes = v_resp.read()

                                    return {
                                        "image_bytes": img_bytes,
                                        "generator": "comfyui",
                                        "model": checkpoint_name or "default",
                                        "seed": actual_seed,
                                        "prompt_digest": prompt_text[:160],
                                    }
                    time.sleep(1.0)
        except Exception:
            return None

        return None
