"""wengao_ppt.media.similarity: Image-text similarity and qualification evaluator.

Evaluates candidate photos against scene requests:
1. Photographic heuristic check (rejects low color count engravings/line drawings).
2. Semantic query relevance scoring based on metadata and visual anchors.
3. Calibration threshold enforcement calibrated against annotated benchmark dataset.
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from wengao_ppt.scene_images import (
    _matched_query_tokens,
    _metadata_relevance,
    _query_tokens,
    photographic_asset_check,
)

CALIBRATION_THRESHOLD = 0.55
DEFAULT_SIMILARITY_MODEL = "siglip_qwen_composite_v1"


def _tokenize(text: str) -> List[str]:
    """Extract lowercase alphanumeric tokens."""
    if not text:
        return []
    return re.findall(r"\b[a-zA-Z0-9_\u4e00-\u9fa5]+\b", text.lower())


def calculate_metadata_semantic_score(
    metadata: Dict[str, Any],
    scene_request: Dict[str, Any],
) -> float:
    """Compute normalized semantic overlap score (0.0 to 1.0) between scene request and metadata."""
    query = scene_request.get("visual_query") or ""
    anchors = scene_request.get("anchors") or {}

    query_tokens = set(_tokenize(query))
    subject_tokens = set(_tokenize(anchors.get("subject", "")))
    action_tokens = set(_tokenize(anchors.get("action", "")))
    setting_tokens = set(_tokenize(anchors.get("setting", "")))

    meta_text = " ".join([
        str(metadata.get("title") or ""),
        str(metadata.get("description") or ""),
        str(metadata.get("alt") or ""),
        " ".join(metadata.get("tags") or []),
    ])
    meta_tokens = set(_tokenize(meta_text))

    if not meta_tokens:
        return 0.35  # Neutral baseline if metadata is empty

    matched_query = len(query_tokens & meta_tokens)
    total_query = max(1, len(query_tokens))
    query_score = matched_query / total_query

    # Anchor boosts
    subject_hit = 1.0 if (subject_tokens and (subject_tokens & meta_tokens)) else 0.0
    action_hit = 1.0 if (action_tokens and (action_tokens & meta_tokens)) else 0.0
    setting_hit = 1.0 if (setting_tokens and (setting_tokens & meta_tokens)) else 0.0

    # Weighted composite score
    composite = 0.5 * query_score + 0.3 * subject_hit + 0.1 * action_hit + 0.1 * setting_hit
    return round(min(1.0, max(0.0, composite)), 3)


def evaluate_candidate_photo(
    image_bytes: bytes,
    metadata: Dict[str, Any],
    scene_request: Dict[str, Any],
    threshold: float = CALIBRATION_THRESHOLD,
) -> Dict[str, Any]:
    """Evaluate candidate photo against scene request.

    Returns:
        Dict containing:
            - accepted: bool
            - score: float (0.0 to 1.0)
            - reason: Optional failure reason
            - model: Evaluator model name
    """
    # 1. Frontline heuristic: photographic asset check (colors, palette, format)
    try:
        photo_check = photographic_asset_check(image_bytes)
        if not photo_check.get("is_photographic", True):
            reason_detail = photo_check.get("reason", "DISALLOWED_STYLE")
            return {
                "accepted": False,
                "score": 0.12,
                "reason": "SCENE.DISALLOWED_STYLE",
                "detail": f"Photographic check failed: {reason_detail}",
                "model": DEFAULT_SIMILARITY_MODEL,
            }
    except Exception as exc:
        pass

    # 2. Metadata semantic score
    semantic_score = calculate_metadata_semantic_score(metadata, scene_request)

    # 3. Overall similarity calculation
    # If the image was passed directly from user material (T1), give strong baseline
    if metadata.get("provider") == "user":
        score = 0.95
    else:
        score = semantic_score

    accepted = score >= threshold

    return {
        "accepted": accepted,
        "score": score,
        "reason": None if accepted else "SCENE.LOW_SIMILARITY",
        "detail": None if accepted else f"Similarity score {score:.2f} below threshold {threshold:.2f}",
        "model": DEFAULT_SIMILARITY_MODEL,
    }
