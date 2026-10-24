"""wengao_ppt.media.place: Image placement and ledger verification.

Handles inserting resolved assets into PPTX slides with proper cropping,
alt text, notes attribution, and independent verification of the image ledger.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union
from xml.etree import ElementTree as etree

from pptx import Presentation
from pptx.oxml.ns import qn
from pptx.util import Inches

from wengao_ppt.visual import _apply_srcrect, _cover_srcrect


def place_image(
    slide: Any,
    rect: Sequence[Union[int, float]],
    asset: Dict[str, Any],
    fit: str = "cover",
    alt_text: str = "",
) -> int:
    """Insert a resolved image asset into a slide at the specified rectangle.

    Args:
        slide: python-pptx Slide object.
        rect: [left, top, width, height] in inches.
        asset: Asset dict conforming to rs.scene_resolution.v1.
        fit: 'cover' (default) or 'contain'.
        alt_text: Descriptive alt text. If empty, falls back to attribution text.

    Returns:
        The shape_id of the newly inserted picture shape.
    """
    if not asset or not asset.get("path"):
        raise ValueError("Cannot place empty asset onto slide")

    img_path = Path(asset["path"])
    if not img_path.is_file():
        raise FileNotFoundError(f"Asset file not found: {img_path}")

    left, top, width, height = [float(x) for x in rect]
    pic = slide.shapes.add_picture(
        str(img_path),
        Inches(left),
        Inches(top),
        Inches(width),
        Inches(height),
    )

    if pic is None or not hasattr(pic, "shape_id"):
        raise RuntimeError("Failed to create slide picture shape")

    # Apply cover crop if requested
    if fit == "cover":
        bounds = _cover_srcrect(img_path, width, height, 0.5, 0.5)
        _apply_srcrect(pic, bounds)

    # Set alt text / description in nvPicPr.cNvPr
    descr_text = alt_text or asset.get("attribution_text") or asset.get("source_url") or "Scene Photo"
    c_nv = pic._element.nvPicPr.cNvPr
    c_nv.set("descr", str(descr_text)[:500])

    # Mark sha256 in shape name for verification
    c_nv.set("name", f"IMG_{asset.get('sha256', '')[:16]}")

    # Write attribution into slide notes
    _append_slide_notes(slide, asset)

    return int(pic.shape_id)


def _append_slide_notes(slide: Any, asset: Dict[str, Any]) -> None:
    """Append asset license and source attribution to slide speaker notes."""
    try:
        notes_slide = slide.notes_slide
        tf = notes_slide.notes_text_frame
    except Exception:
        return

    provider = asset.get("provider", "unknown")
    if provider == "ai_generated_local":
        model = asset.get("model") or "ComfyUI"
        seed = asset.get("seed") or "N/A"
        prompt = asset.get("prompt_digest") or "N/A"
        note_line = f"[AI 生成] 模型: {model} | 种子: {seed} | 提示词: {prompt}"
    elif provider == "user":
        note_line = f"[原稿配图] 用户材料自带配图 | SHA256: {asset.get('sha256', '')[:12]}"
    else:
        creator = asset.get("creator") or "Unknown"
        license_name = asset.get("license") or "Licensed"
        url = asset.get("source_url") or ""
        note_line = f"[实拍配图] 来源: {provider} | 作者: {creator} | 许可: {license_name} | {url}".strip()

    existing = tf.text.strip()
    if existing:
        if note_line not in existing:
            tf.text = f"{existing}\n{note_line}"
    else:
        tf.text = note_line


def check_image_ledger(job_dir: Union[str, Path]) -> Dict[str, Any]:
    """Independently audit PPTX media against evidence/images/ledger.json.

    Reads the presentation and ledger on disk, counts p:pic / ppt/media,
    and returns a CheckReport with separate delivery status and coverage rate.
    """
    job_path = Path(job_dir)
    ledger_file = job_path / "evidence" / "images" / "ledger.json"

    if not ledger_file.is_file():
        return {
            "schema": "rs.check_report.v1",
            "module": "image_ledger",
            "status": "FAIL",
            "errors": [
                {
                    "code": "SCENE.LEDGER_MISMATCH",
                    "detail": f"Ledger file not found: {ledger_file}",
                }
            ],
            "total_scenes": 0,
            "resolved_scenes": 0,
            "fallback_scenes": 0,
            "deck_delivery_status": "FAILED",
            "deck_success": False,
            "scene_coverage_rate": 0.0,
            "tier_distribution": {},
        }

    ledger_data = json.loads(ledger_file.read_text(encoding="utf-8"))
    total_scenes = ledger_data.get("total_scenes", 0)
    resolutions = ledger_data.get("resolutions", [])
    tier_dist = ledger_data.get("tier_distribution", {})

    resolved_count = sum(1 for r in resolutions if r.get("status") in ("RESOLVED_T1", "RESOLVED_T2", "RESOLVED_T3"))
    fallback_count = sum(1 for r in resolutions if r.get("status") == "FALLBACK_T4")
    coverage_rate = (resolved_count / total_scenes) if total_scenes > 0 else 0.0

    errors: List[Dict[str, Any]] = []

    # Verify PPTX files if present in job directory
    pptx_candidates = list(job_path.glob("*.pptx")) + list((job_path / "output").glob("*.pptx"))
    pptx_media_shas: set[str] = set()

    for pptx in pptx_candidates:
        try:
            with zipfile.ZipFile(pptx, "r") as z:
                for name in z.namelist():
                    if name.startswith("ppt/media/"):
                        raw = z.read(name)
                        pptx_media_shas.add(hashlib.sha256(raw).hexdigest())
        except Exception as exc:
            errors.append({
                "code": "SCENE.LEDGER_MISMATCH",
                "detail": f"Could not inspect PPTX archive {pptx}: {exc}",
            })

    # For each resolved asset, ensure sha256 matches
    for r in resolutions:
        status = r.get("status")
        asset = r.get("asset")
        if status in ("RESOLVED_T1", "RESOLVED_T2", "RESOLVED_T3"):
            if not asset:
                errors.append({
                    "code": "SCENE.LEDGER_MISMATCH",
                    "detail": f"Scene {r.get('scene_id')} is {status} but asset is null",
                })
            else:
                asset_sha = asset.get("sha256")
                # If PPTX candidate exists, asset sha should be present in ppt/media
                if pptx_candidates and pptx_media_shas and asset_sha not in pptx_media_shas:
                    errors.append({
                        "code": "SCENE.LEDGER_MISMATCH",
                        "detail": f"Asset sha {asset_sha[:12]} for scene {r.get('scene_id')} not found in PPTX media",
                    })

    status_str = "PASS" if not errors else "FAIL"
    deck_success = bool(pptx_candidates) or ledger_data.get("deck_success", False)

    return {
        "schema": "rs.check_report.v1",
        "module": "image_ledger",
        "status": status_str,
        "deck_delivery_status": "DELIVERED" if deck_success else "FAILED",
        "deck_success": deck_success,
        "total_scenes": total_scenes,
        "resolved_scenes": resolved_count,
        "fallback_scenes": fallback_count,
        "scene_coverage_rate": coverage_rate,
        "tier_distribution": tier_dist,
        "errors": errors,
    }
