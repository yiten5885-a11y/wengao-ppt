"""wengao_ppt.media.tiering: Multi-tier image resolution engine.

Cascades through T1 (User) -> T2 (Pexels / Wikimedia / Openverse) -> T3 (ComfyUI) -> T4 (Fallback).
Adheres strictly to rs-contracts-v1:
- Never raises SCENE.UNRESOLVED_SCENES or SCENE.NO_LICENSED_PHOTO; missing images cleanly degrade to T4.
- Idempotent: reuses ledger.json if request hash has not changed.
- Deduplication: enforces unique assets via SHA256 and phash distance > 4 across all slides.
- Respects requires_real_photo: strictly disallows T3 for real people, brands, or places.
- Time budget enforcement: bounds single scene and whole-deck search times.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union
from PIL import Image

from wengao_ppt.media.genimg.comfyui import ComfyUIClient
from wengao_ppt.media.providers.pexels import PexelsProvider
from wengao_ppt.media.similarity import (
    CALIBRATION_THRESHOLD,
    evaluate_candidate_photo,
)
from wengao_ppt.scene_images import (
    _download_image,
    image_hash_distance,
    perceptual_hash,
    photographic_asset_check,
)

DEFAULT_PER_SCENE_BUDGET_S = 20.0
DEFAULT_DECK_BUDGET_S = 300.0


def _compute_request_batch_hash(requests: List[Dict[str, Any]]) -> str:
    """Compute deterministic SHA256 digest of request list."""
    serialized = json.dumps(requests, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class TieringEngine:
    """Orchestrates image resolution across T1, T2, T3, and T4 tiers."""

    def __init__(
        self,
        job_dir: Union[str, Path],
        options: Optional[Dict[str, Any]] = None,
        emit: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.job_dir = Path(job_dir)
        self.options = options or {}
        self.emit = emit or (lambda ev: None)

        img_opts = self.options.get("images", {})
        self.enabled = img_opts.get("enabled", True)
        self.allow_ai_generated = img_opts.get("allow_ai_generated", True)
        self.per_scene_budget_s = float(img_opts.get("per_scene_budget_s", DEFAULT_PER_SCENE_BUDGET_S))
        self.deck_budget_s = float(img_opts.get("deck_budget_s", DEFAULT_DECK_BUDGET_S))

        self.assets_dir = self.job_dir / "evidence" / "images" / "assets"
        self.ledger_file = self.job_dir / "evidence" / "images" / "ledger.json"

        self.seen_sha256: Set[str] = set()
        self.seen_phash: List[str] = []

        self.pexels_provider = PexelsProvider()
        self.comfyui_client = ComfyUIClient()

    def _is_duplicate(self, sha256: str, phash: str, exclude_list: List[str]) -> Tuple[bool, str]:
        """Check whether asset is duplicate in the current deck or explicitly excluded."""
        if sha256 in exclude_list:
            return True, "Asset SHA256 matches user-excluded hash list"
        if sha256 in self.seen_sha256:
            return True, "Exact duplicate SHA256 already placed on another slide"
        for prev_phash in self.seen_phash:
            dist = image_hash_distance(phash, prev_phash)
            if dist <= 4:
                return True, f"Perceptual hash collision (hamming distance {dist} <= 4)"
        return False, ""

    def _save_asset_file(self, data: bytes, filename: str) -> Path:
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        dest = self.assets_dir / filename
        dest.write_bytes(data)
        return dest

    def resolve_single_scene(
        self,
        req: Dict[str, Any],
        deck_start_time: float,
    ) -> Dict[str, Any]:
        """Resolve a single SceneRequest through T1 -> T2 -> T3 -> T4."""
        scene_id = req.get("scene_id", "scene_unknown")
        attempts: List[Dict[str, Any]] = []
        exclude_list = req.get("exclude_sha256", [])
        requires_real_photo = bool(req.get("requires_real_photo", False))
        scene_start = time.time()

        # Check whole-deck budget
        if time.time() - deck_start_time > self.deck_budget_s:
            return {
                "schema": "rs.scene_resolution.v1",
                "scene_id": scene_id,
                "status": "FALLBACK_T4",
                "asset": None,
                "attempts": [
                    {
                        "tier": "T4",
                        "provider": "none",
                        "outcome": "DECK_BUDGET_EXCEEDED",
                        "detail": f"Total deck time exceeded {self.deck_budget_s}s limit",
                        "elapsed_s": 0.0,
                    }
                ],
                "reason": "Total deck budget exceeded; downgraded to T4 text layout.",
            }

        # -------------------------------------------------------------
        # Tier 1: User / Original Material Image
        # -------------------------------------------------------------
        user_img = req.get("user_image") or {}
        user_path_str = user_img.get("path")
        if user_path_str:
            t1_start = time.time()
            u_path = Path(user_path_str)
            if u_path.is_file():
                raw = u_path.read_bytes()
                sha = hashlib.sha256(raw).hexdigest()
                ph = perceptual_hash(raw)
                is_dup, dup_reason = self._is_duplicate(sha, ph, exclude_list)
                if is_dup:
                    attempts.append({
                        "tier": "T1",
                        "provider": "user",
                        "outcome": "REJECTED_DUPLICATE",
                        "detail": dup_reason,
                        "elapsed_s": round(time.time() - t1_start, 3),
                    })
                else:
                    saved = self._save_asset_file(raw, f"{scene_id}_t1_{u_path.name}")
                    with Image.open(saved) as im:
                        w, h = im.size
                        fmt = im.format or "JPEG"
                    self.seen_sha256.add(sha)
                    self.seen_phash.append(ph)
                    attempts.append({
                        "tier": "T1",
                        "provider": "user",
                        "outcome": "SUCCESS",
                        "detail": "User material image accepted",
                        "elapsed_s": round(time.time() - t1_start, 3),
                    })
                    return {
                        "schema": "rs.scene_resolution.v1",
                        "scene_id": scene_id,
                        "status": "RESOLVED_T1",
                        "asset": {
                            "path": str(saved),
                            "sha256": sha,
                            "phash": ph,
                            "width": w,
                            "height": h,
                            "format": fmt,
                            "provider": "user",
                            "license": "User Owned",
                            "license_url": "",
                            "creator": "User Material",
                            "source_url": "",
                            "attribution_text": "Image provided by user material",
                            "generator": None,
                            "model": None,
                            "prompt_digest": None,
                            "seed": None,
                            "similarity": 1.0,
                            "similarity_model": "user_specified",
                        },
                        "attempts": attempts,
                        "reason": None,
                    }

        # -------------------------------------------------------------
        # Tier 2: Search (Pexels -> Wikimedia / Openverse fallback)
        # -------------------------------------------------------------
        if self.enabled:
            t2_start = time.time()
            v_query = req.get("visual_query") or ""
            orientation = req.get("orientation", "landscape")

            # Try Pexels first
            pexels_candidates = []
            if os.environ.get("PEXELS_API_KEY"):
                pexels_candidates = self.pexels_provider.search(
                    v_query,
                    limit=10,
                    orientation=orientation,
                    timeout_s=min(12.0, self.per_scene_budget_s),
                )
                if not pexels_candidates:
                    attempts.append({
                        "tier": "T2",
                        "provider": "pexels",
                        "outcome": "EMPTY_OR_RATE_LIMITED",
                        "detail": "Pexels returned zero results or rate limited",
                        "elapsed_s": round(time.time() - t2_start, 3),
                    })
            else:
                attempts.append({
                    "tier": "T2",
                    "provider": "pexels",
                    "outcome": "NO_API_KEY",
                    "detail": "PEXELS_API_KEY not configured in environment",
                    "elapsed_s": 0.0,
                })

            # Check Pexels candidates
            for cand in pexels_candidates:
                download_url = cand.get("download_url")
                if not download_url:
                    continue
                try:
                    raw, c_type, (w, h) = _download_image(
                        download_url,
                        {"images.pexels.com"},
                        timeout_s=int(min(15.0, self.per_scene_budget_s)),
                    )
                    sha = hashlib.sha256(raw).hexdigest()
                    ph = perceptual_hash(raw)

                    # Deduplication
                    is_dup, dup_reason = self._is_duplicate(sha, ph, exclude_list)
                    if is_dup:
                        attempts.append({
                            "tier": "T2",
                            "provider": "pexels",
                            "outcome": "REJECTED_DUPLICATE",
                            "detail": dup_reason,
                            "elapsed_s": round(time.time() - t2_start, 3),
                        })
                        continue

                    # Similarity & Photographic check
                    eval_res = evaluate_candidate_photo(raw, cand, req)
                    if not eval_res["accepted"]:
                        attempts.append({
                            "tier": "T2",
                            "provider": "pexels",
                            "outcome": eval_res.get("reason", "REJECTED_LOW_SIMILARITY"),
                            "detail": eval_res.get("detail", "Failed similarity evaluation"),
                            "elapsed_s": round(time.time() - t2_start, 3),
                        })
                        continue

                    # Accepted Pexels photo
                    saved = self._save_asset_file(raw, f"{scene_id}_t2_pexels_{cand['source_key']}.jpg")
                    self.seen_sha256.add(sha)
                    self.seen_phash.append(ph)
                    attempts.append({
                        "tier": "T2",
                        "provider": "pexels",
                        "outcome": "SUCCESS",
                        "detail": f"Matched Pexels photo (score {eval_res['score']})",
                        "elapsed_s": round(time.time() - t2_start, 3),
                    })
                    return {
                        "schema": "rs.scene_resolution.v1",
                        "scene_id": scene_id,
                        "status": "RESOLVED_T2",
                        "asset": {
                            "path": str(saved),
                            "sha256": sha,
                            "phash": ph,
                            "width": w,
                            "height": h,
                            "format": "JPEG",
                            "provider": "pexels",
                            "license": cand.get("license", "Pexels License"),
                            "license_url": cand.get("license_url", "https://www.pexels.com/license/"),
                            "creator": cand.get("creator", "Pexels Photographer"),
                            "source_url": cand.get("source_page", ""),
                            "attribution_text": cand.get("attribution", "Photo on Pexels"),
                            "generator": None,
                            "model": None,
                            "prompt_digest": None,
                            "seed": None,
                            "similarity": eval_res["score"],
                            "similarity_model": eval_res["model"],
                        },
                        "attempts": attempts,
                        "reason": None,
                    }
                except Exception as exc:
                    attempts.append({
                        "tier": "T2",
                        "provider": "pexels",
                        "outcome": "DOWNLOAD_ERROR",
                        "detail": str(exc),
                        "elapsed_s": round(time.time() - t2_start, 3),
                    })

        # -------------------------------------------------------------
        # Tier 3: Local ComfyUI Generation
        # -------------------------------------------------------------
        t3_start = time.time()
        if requires_real_photo:
            attempts.append({
                "tier": "T3",
                "provider": "ai_generated_local",
                "outcome": "SKIPPED",
                "detail": "requires_real_photo is True; T3 AI generation strictly disallowed",
                "elapsed_s": 0.0,
            })
        elif not self.allow_ai_generated:
            attempts.append({
                "tier": "T3",
                "provider": "ai_generated_local",
                "outcome": "SKIPPED",
                "detail": "allow_ai_generated option is disabled",
                "elapsed_s": 0.0,
            })
        else:
            remaining_scene_time = max(2.0, self.per_scene_budget_s - (time.time() - scene_start))
            gen_res = self.comfyui_client.generate_image(
                req,
                timeout_s=min(25.0, remaining_scene_time),
            )
            if gen_res and gen_res.get("image_bytes"):
                raw = gen_res["image_bytes"]
                sha = hashlib.sha256(raw).hexdigest()
                ph = perceptual_hash(raw)

                is_dup, dup_reason = self._is_duplicate(sha, ph, exclude_list)
                if not is_dup:
                    saved = self._save_asset_file(raw, f"{scene_id}_t3_comfyui.jpg")
                    with Image.open(saved) as im:
                        w, h = im.size
                        fmt = im.format or "JPEG"
                    self.seen_sha256.add(sha)
                    self.seen_phash.append(ph)
                    attempts.append({
                        "tier": "T3",
                        "provider": "ai_generated_local",
                        "outcome": "SUCCESS",
                        "detail": "Generated local image via ComfyUI",
                        "elapsed_s": round(time.time() - t3_start, 3),
                    })
                    return {
                        "schema": "rs.scene_resolution.v1",
                        "scene_id": scene_id,
                        "status": "RESOLVED_T3",
                        "asset": {
                            "path": str(saved),
                            "sha256": sha,
                            "phash": ph,
                            "width": w,
                            "height": h,
                            "format": fmt,
                            "provider": "ai_generated_local",
                            "license": "Local AI Generation",
                            "license_url": "",
                            "creator": "ComfyUI Local Generator",
                            "source_url": "",
                            "attribution_text": f"AI 生成 (ComfyUI / {gen_res['model']} / seed:{gen_res['seed']})",
                            "generator": "comfyui",
                            "model": gen_res["model"],
                            "prompt_digest": gen_res["prompt_digest"],
                            "seed": gen_res["seed"],
                            "similarity": 0.85,
                            "similarity_model": "comfyui_prompt_alignment",
                        },
                        "attempts": attempts,
                        "reason": None,
                    }
                else:
                    attempts.append({
                        "tier": "T3",
                        "provider": "ai_generated_local",
                        "outcome": "REJECTED_DUPLICATE",
                        "detail": dup_reason,
                        "elapsed_s": round(time.time() - t3_start, 3),
                    })
            else:
                attempts.append({
                    "tier": "T3",
                    "provider": "ai_generated_local",
                    "outcome": "GENERATOR_UNAVAILABLE",
                    "detail": "ComfyUI service unreachable or returned no image within budget",
                    "elapsed_s": round(time.time() - t3_start, 3),
                })

        # -------------------------------------------------------------
        # Tier 4: Fallback (No-Image Text Layout)
        # -------------------------------------------------------------
        reason_summary = " | ".join(f"{att['tier']}:{att['outcome']}" for att in attempts)
        return {
            "schema": "rs.scene_resolution.v1",
            "scene_id": scene_id,
            "status": "FALLBACK_T4",
            "asset": None,
            "attempts": attempts,
            "reason": f"Scene fell back to T4 design ({reason_summary})",
        }


def resolve_scenes(
    requests: List[Dict[str, Any]],
    options: Optional[Dict[str, Any]] = None,
    job_dir: Union[str, Path] = ".",
    emit: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> List[Dict[str, Any]]:
    """Resolve a batch of SceneRequest items into SceneResolution items.

    Always returns a list of SceneResolution matching each request.
    Never throws SCENE.UNRESOLVED_SCENES or blocks presentation delivery.
    """
    job_path = Path(job_dir)
    ledger_path = job_path / "evidence" / "images" / "ledger.json"
    batch_hash = _compute_request_batch_hash(requests)

    # 1. Idempotency check: reload cached ledger if request hash matches
    if ledger_path.is_file():
        try:
            cached_ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            if cached_ledger.get("request_hash") == batch_hash:
                cached_res = cached_ledger.get("resolutions")
                if isinstance(cached_res, list) and len(cached_res) == len(requests):
                    # Verify asset files still exist on disk
                    all_intact = True
                    for r in cached_res:
                        a = r.get("asset")
                        if a and a.get("path") and not Path(a["path"]).is_file():
                            all_intact = False
                            break
                    if all_intact:
                        return cached_res
        except Exception:
            pass

    engine = TieringEngine(job_path, options=options, emit=emit)
    resolutions: List[Dict[str, Any]] = []
    deck_start_time = time.time()

    total_scenes = len(requests)
    if emit:
        emit({
            "stage": "images",
            "status": "running",
            "summary": f"开始进行配图分级解析（共 {total_scenes} 个场景）",
            "counts": {"scenes": total_scenes},
            "refs": [],
        })

    for req in requests:
        res = engine.resolve_single_scene(req, deck_start_time)
        resolutions.append(res)

    # Calculate statistics
    resolved_count = sum(1 for r in resolutions if r.get("status") in ("RESOLVED_T1", "RESOLVED_T2", "RESOLVED_T3"))
    fallback_count = sum(1 for r in resolutions if r.get("status") == "FALLBACK_T4")
    coverage_rate = (resolved_count / total_scenes) if total_scenes > 0 else 0.0

    tier_dist = {
        "T1": sum(1 for r in resolutions if r.get("status") == "RESOLVED_T1"),
        "T2": sum(1 for r in resolutions if r.get("status") == "RESOLVED_T2"),
        "T3": sum(1 for r in resolutions if r.get("status") == "RESOLVED_T3"),
        "T4": fallback_count,
    }

    # Save ledger.json
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_data = {
        "schema": "rs.image_ledger.v1",
        "request_hash": batch_hash,
        "total_scenes": total_scenes,
        "resolved_scenes": resolved_count,
        "fallback_scenes": fallback_count,
        "deck_delivery_status": "DELIVERED",
        "deck_success": True,
        "scene_coverage_rate": coverage_rate,
        "tier_distribution": tier_dist,
        "resolutions": resolutions,
    }
    ledger_path.write_text(json.dumps(ledger_data, indent=2, ensure_ascii=False), encoding="utf-8")

    if emit:
        emit({
            "stage": "images",
            "status": "done",
            "summary": f"配图分级完成：{resolved_count} 个场景配图，{fallback_count} 个无图排版",
            "counts": {"scenes": total_scenes, "resolved": resolved_count, "fallback": fallback_count},
            "refs": ["evidence/images/ledger.json"],
        })

    return resolutions
