"""Open-vocabulary, deck-wide semantic scene planning and hard validation.

The local model plans concrete people, objects, activities, and places from each
source deck. A specific scene is deduplicated across slides, while distinct
sub-scenes in the same broad topic may each receive one photograph. Photos must
come from a license-filtered provider and occupy a safe picture area or replace
an existing generic image slot; a missing photo or safe placement blocks output.
"""
from __future__ import annotations

import hashlib
import inspect
import io
import json
import math
import os
import re
import tempfile
import time
import unicodedata
import zipfile
from collections import Counter
from pathlib import Path

from PIL import Image, ImageChops, ImageStat
from lxml import etree
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE, MSO_SHAPE_TYPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.dml.color import RGBColor
from pptx.util import Inches, Pt

from . import visual as visual_mod
from .type_scale import ROLE_RANGE_PT
from .scene_images import (SceneImageError, ScenePhotoProvider, is_allowed_license,
                           is_safe_metadata, is_valid_license_url, metadata_relevance,
                           scene_metadata_relevance,
                           build_provider_search_query_from_anchors,
                           normalize_photo_search_terms,
                           is_neutral_photo_search_query,
                           query_preserves_photo_search_anchors,
                           photo_style_matches_metadata, scene_metadata_matches_query,
                           MIN_METADATA_RELEVANCE, MIN_IMAGE_WIDTH, MIN_IMAGE_HEIGHT,
                           photographic_asset_check,
                           perceptual_hash, image_hash_distance,
                           review_missing_role,
                           is_valid_pexels_source_page, is_valid_pixabay_source_page)
from .scene_model import (SceneModelError, classify_external_model_failure,
                          resolve_scene_semantics, scene_coverage_audit_prompt,
                          call_local_model, preflight_scene_models,
                          _call_json_payload_once_on_format_error,
                          _validate_scene_coverage_audit, _coverage_plan_view,
                          scene_review_verdict_digest,
                          _merge_scene_conflict_patch, _scene_merge_conflict_components,
                          _apply_grounded_scene_gap_repairs,
                          scene_gap_repair_prompt,
                          _validate_scene_gap_repair_scope,
                          scene_merge_replan_prompt,
                          _validate_scene_merge_replan_scope,
                          _enrich_scene_image_prompts,
                          _repair_binding_payload,
                          _repair_binding_sha256,
                          review_semantic_scene_image,
                          scene_visual_identity_sha256,
                          scene_image_review_prompt,
                          scene_review_payload_sha256,
                          scene_review_result_sha256)
from .language_support import detect_language_hint, language_support_record

__all__ = ["SemanticSceneError", "SceneModelError", "classify_external_model_failure",
           "build_plan", "apply_plan", "validate_output",
           "validate_rendered_scene_visibility", "validate_complete_output",
           "make_scene_suppressed_pptx", "reconcile_visual_plan"]

VERSION = "2.22.0"
POLICY_FILE = Path(__file__).resolve().parents[1] / "contracts" / "semantic_scene_gate_policy.json"
POLICY_SHA256 = "e9aaeac53b8f242af5138e3c018fe7684487ee8935e30d16e97da660f584c41a"
SCENE_GATE_ID = "HG13_SEMANTIC_SCENE_COVERAGE"
_IN = 914400.0
_MIN_AREA_RATIO = 0.12
_MAX_TEXT_OVERLAP = 0.28
_MAX_EMPTY_AREA_TEXT_OVERLAP = 0.035
_MAX_SCENES_PER_PAGE = 2
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_]{1,79}$")
_COVER_CROP_TOLERANCE = 0.02
_SCENE_RENDER_EVIDENCE_SCHEMA = "WG-SEMANTIC-SCENE-RENDER-EVIDENCE/1"
_SCENE_RENDER_GRID = 8
_SCENE_RENDER_MAX_TILE_MAE = 48.0
_SCENE_RENDER_MIN_MATCHED_TILES = 0.65
_SCENE_RENDER_MIN_CHANGED_PIXEL_FRACTION = 0.65
_SCENE_RENDER_MIN_WIDTH = 640
_SCENE_RENDER_MIN_HEIGHT = 360
_SCENE_RENDER_MIN_SOURCE_RETAINED = 0.58


class SemanticSceneError(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = str(detail)[:800]


def _sha(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _bytes_sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_sha(value: dict) -> str:
    return _bytes_sha(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":")).encode("utf-8"))


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _source_pages(pptx_path: str | Path) -> list[dict]:
    prs = Presentation(str(pptx_path))
    pages = []
    for number, slide in enumerate(prs.slides, 1):
        rows = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                text = (shape.text_frame.text or "").strip()
                if text:
                    rows.append((float(shape.top or 0), float(shape.left or 0), text))
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [str(cell.text or "").strip() for cell in row.cells]
                    if any(cells):
                        rows.append((float(shape.top or 0), float(shape.left or 0),
                                     " | ".join(cells)))
        rows.sort(key=lambda item: (item[0], item[1]))
        chunks = [item[2] for item in rows]
        text = "\n".join(chunks)
        title = next((item[2] for item in rows if item[0] / _IN <= 1.55),
                     chunks[0] if chunks else "")
        pages.append({"page": number, "title": title, "text": text,
                      "language_hint": detect_language_hint(text),
                      "text_sha256": _bytes_sha(text.encode("utf-8")),
                      "text_chars": len(text)})
    return pages


def _excerpt_matches(source_text: str, excerpt: str) -> bool:
    e = _norm(excerpt)
    return bool(e) and e in _norm(source_text)


def _normalize_canonical_scene_token(value: object) -> str:
    """Normalize a canonical scene id for dedupe without merging sub-scenes.

    Two model responses can describe the same concrete scene as
    ``consumer_checkout`` and ``consumer.checkouts`` or
    ``supermarket-shopping`` and ``supermarket_shopping``. Those are one
    picture and must dedupe to one placement. Normalization is deliberately
    limited to case, separators, repeated underscores and simple English
    plurals: it never rewrites the head noun, so ``consumer.checkout`` and
    ``consumer.receipt`` stay distinct even though both belong to the broad
    topic "consumption".
    """
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    text = re.sub(r"[\s\-–—/\\|.]+", "_", text)
    text = re.sub(r"[^a-z0-9_]+", "", text)
    segments = []
    for segment in re.sub(r"_+", "_", text).strip("_").split("_"):
        if not segment:
            continue
        if len(segment) > 4 and segment.endswith("ies"):
            segment = segment[:-3] + "y"
        elif len(segment) > 4 and segment.endswith("ses"):
            segment = segment[:-2]
        elif len(segment) > 3 and segment.endswith("s") and not segment.endswith("ss"):
            segment = segment[:-1]
        segments.append(segment)
    return "_".join(segments)


def _identity_signature(scene: dict) -> str:
    canonical = _normalize_canonical_scene_token(scene.get("canonical_scene_id"))
    if canonical:
        return "canonical:" + canonical
    return _localized_identity_signature(scene)


def _localized_identity_signature(scene: dict) -> str:
    parts = [_norm(scene.get(k, "")) for k in ("subject", "action", "setting")]
    return "|".join(parts)


_GENERIC_NO_SCENE_REASONS = {
    _norm("本页为宏观概念论述或无具体实体场景"),
    _norm("无场景"), _norm("没有场景"), _norm("不适合配图"),
    _norm("无合适图片"), _norm("no scene"), _norm("no suitable scene"),
    _norm("not suitable for images"),
}


def _is_explicit_no_scene_reason(value: object) -> bool:
    """Require an auditable page-specific explanation, not a filler default."""
    reason = str(value or "").strip()
    compact = re.sub(r"\s+", "", reason)
    return (len(compact) >= 12
            and _norm(reason) not in _GENERIC_NO_SCENE_REASONS)


def _normalize_semantics(raw: dict, pages: list[dict], *,
                         require_canonical_scene_id: bool = False,
                         dedupe_audit: list | None = None) -> tuple[list[dict], list[dict]]:
    if not isinstance(raw, dict):
        raise SemanticSceneError("SCENE.MODEL_SCHEMA_INVALID", "场景模型结果不是字典。")
    for k in ("scene_plan", "plan", "data", "result", "output", "semantic_scenes"):
        if isinstance(raw.get(k), dict) and any(sk in raw[k] for sk in ("scenes", "scene", "page_coverage")):
            raw = raw[k]
            break
    scenes_list = raw.get("scenes")
    if scenes_list is None:
        if require_canonical_scene_id:
            raise SemanticSceneError("SCENE.MODEL_SCHEMA_INVALID",
                                     "真实模型结果必须显式包含 scenes 数组。")
        scenes_list = raw.get("scene") or raw.get("scene_list")
    if isinstance(scenes_list, dict) and not require_canonical_scene_id:
        scenes_list = list(scenes_list.values())
    cov_list = (raw.get("page_coverage") if require_canonical_scene_id else
                (raw.get("page_coverage") or raw.get("pages")))
    source_by_page = {int(p["page"]): p for p in pages}
    if require_canonical_scene_id and not isinstance(scenes_list, list):
        raise SemanticSceneError("SCENE.MODEL_SCHEMA_INVALID",
                                 "真实模型结果的 scenes 必须是 JSON 数组。")
    if (not require_canonical_scene_id
            and (not isinstance(scenes_list, list) or not scenes_list)
            and isinstance(cov_list, list) and cov_list):
        inferred_scenes = {}
        for row in cov_list:
            try:
                p_no = int(row.get("page"))
            except (TypeError, ValueError):
                continue
            keys = row.get("scene_keys") or []
            ev = row.get("evidence") or []
            for k in keys:
                if not isinstance(k, str) or not k.strip():
                    continue
                k_clean = k.strip().lower()
                if k_clean not in inferred_scenes:
                    words = k_clean.replace("_", " ").strip()
                    inferred_scenes[k_clean] = {
                        "scene_key": k_clean,
                        "canonical_scene_id": k_clean,
                        "topic": words,
                        "subject": words,
                        "action": words,
                        "setting": words,
                        "label": words,
                        "visual_query": words,
                        "visual_query_alternatives": [],
                        "page_candidates": []
                    }
                valid_ev = [str(x).strip() for x in ev if str(x).strip() and p_no in source_by_page and _excerpt_matches(source_by_page[p_no]["text"], str(x).strip())]
                if not valid_ev and p_no in source_by_page:
                    first_line = source_by_page[p_no]["text"].strip().split("\n")[0][:40]
                    valid_ev = [first_line]
                cand_entry = next((c for c in inferred_scenes[k_clean]["page_candidates"] if c["page"] == p_no), None)
                if cand_entry:
                    cand_entry["evidence"] = list(dict.fromkeys(cand_entry["evidence"] + valid_ev))
                else:
                    inferred_scenes[k_clean]["page_candidates"].append({
                        "page": p_no,
                        "relevance": 0.9,
                        "evidence": valid_ev
                    })
        scenes_list = list(inferred_scenes.values())
    if not isinstance(scenes_list, list):
        raise SemanticSceneError("SCENE.MODEL_SCHEMA_INVALID",
                                 "场景模型结果必须包含 scenes。")
    if not isinstance(cov_list, list) or not cov_list:
        if require_canonical_scene_id:
            raise SemanticSceneError("SCENE.PAGE_COVERAGE_MISSING",
                                     "真实模型结果必须显式提供完整的 page_coverage 数组。")
        cov_list = []
        for p in pages:
            p_no = int(p["page"])
            covered_keys = []
            ev_list = []
            for sc in scenes_list:
                k = sc.get("scene_key") or sc.get("canonical_scene_id")
                cands = sc.get("page_candidates") or []
                for cand in cands:
                    if cand.get("page") == p_no:
                        if k:
                            covered_keys.append(k)
                        ev = cand.get("evidence") or []
                        ev_list.extend(ev)
            src_text = source_by_page[p_no]["text"].strip()
            first_sent = src_text.split("\n")[0][:40] if src_text else ""
            cov_list.append({
                "page": p_no,
                "scene_keys": list(dict.fromkeys(covered_keys)),
                "no_scene_reason": None if covered_keys else "本页为宏观概念论述或无具体实体场景",
                "evidence": list(dict.fromkeys(ev_list)) if covered_keys else ([first_sent] if first_sent else [])
            })
    raw_coverage = {}
    for idx, row in enumerate(cov_list, 1):
        if not isinstance(row, dict):
            raise SemanticSceneError("SCENE.PAGE_COVERAGE_INVALID",
                                     "逐页场景账本的每一行都必须是 JSON 对象。")
        if require_canonical_scene_id:
            if "page" not in row:
                raise SemanticSceneError("SCENE.PAGE_COVERAGE_INVALID",
                                         "真实模型的逐页账本必须显式填写整数 page。")
            page = row.get("page")
            if isinstance(page, bool) or not isinstance(page, (int, str)):
                raise SemanticSceneError("SCENE.PAGE_COVERAGE_INVALID",
                                         "真实模型的逐页账本 page 必须是整数。")
        else:
            page = row.get("page") or row.get("page_no") or row.get("page_num")
        if page is None and not require_canonical_scene_id:
            for k in row:
                m = re.search(r"page[_\s]*(\d+)", str(k), re.I)
                if m:
                    page = int(m.group(1))
                    break
        if page is None and not require_canonical_scene_id and 1 <= idx <= len(pages):
            page = idx
        try:
            page = int(page)
        except (TypeError, ValueError, AttributeError):
            raise SemanticSceneError("SCENE.PAGE_COVERAGE_INVALID", "逐页场景账本含无效页码。")
        if page not in source_by_page:
            if require_canonical_scene_id:
                raise SemanticSceneError("SCENE.PAGE_COVERAGE_INVALID",
                                         "真实模型的逐页账本包含越界页码；不按原文猜测或重映射。")
            matched_p = None
            ev_list = row.get("evidence") or []
            for p_cand, p_data in source_by_page.items():
                if any(_excerpt_matches(p_data["text"], str(x)) for x in ev_list):
                    matched_p = p_cand
                    break
            if matched_p is not None:
                page = matched_p
                row["page"] = page
            elif len(raw_coverage) >= len(source_by_page):
                continue
            else:
                raise SemanticSceneError("SCENE.PAGE_COVERAGE_INVALID", "逐页场景账本存在越界页码。")
        if page in raw_coverage and require_canonical_scene_id:
            raise SemanticSceneError("SCENE.PAGE_COVERAGE_DUPLICATED",
                                     f"真实模型逐页账本重复登记第 {page} 页。")
        if page in raw_coverage:
            prev = raw_coverage[page]
            row_keys = row.get("scene_keys") or row.get("scene_key") or []
            if isinstance(row_keys, str):
                row_keys = [row_keys]
            valid_row_keys = [k.strip().lower() for k in row_keys if isinstance(k, str) and _KEY_RE.fullmatch(k.strip().lower())]
            prev["scene_keys"] = list(dict.fromkeys(prev.get("scene_keys", []) + valid_row_keys))
            new_ev = [str(x) for x in (row.get("evidence") or []) if _excerpt_matches(source_by_page[page]["text"], str(x))]
            prev["evidence"] = list(dict.fromkeys(prev.get("evidence", []) + new_ev))
            continue
        if require_canonical_scene_id:
            keys = row.get("scene_keys")
            if not isinstance(keys, list):
                raise SemanticSceneError("SCENE.PAGE_COVERAGE_INVALID",
                                         f"真实模型第 {page} 页必须显式提供 scene_keys 数组。")
            if any(not isinstance(k, str) or not _KEY_RE.fullmatch(k.strip().lower())
                   for k in keys):
                raise SemanticSceneError("SCENE.PAGE_COVERAGE_INVALID",
                                         f"真实模型第 {page} 页包含无效场景键，不能静默过滤。")
            normalized_keys = [k.strip().lower() for k in keys]
            if len(normalized_keys) != len(set(normalized_keys)):
                raise SemanticSceneError("SCENE.PAGE_COVERAGE_DUPLICATED",
                                         f"真实模型第 {page} 页重复登记同一场景键。")
            keys = normalized_keys
        else:
            keys = row.get("scene_keys") or row.get("scene_key") or []
            if isinstance(keys, str):
                keys = [keys]
            keys = [k.strip().lower() for k in keys if isinstance(k, str) and _KEY_RE.fullmatch(k.strip().lower())]
        reason = row.get("no_scene_reason")
        evidence = row.get("evidence") or []
        if keys and reason:
            if require_canonical_scene_id and str(reason).strip():
                raise SemanticSceneError("SCENE.PAGE_COVERAGE_CONFLICT",
                                         f"真实模型第 {page} 页同时登记了场景和 no_scene_reason。")
            reason = None
        if not keys:
            if require_canonical_scene_id:
                if not _is_explicit_no_scene_reason(reason):
                    raise SemanticSceneError("SCENE.PAGE_NO_SCENE_REASON_INVALID",
                                             f"真实模型第 {page} 页没有具体场景，必须提供非模板化、可审计的原因。")
                # 原文为空白页（无可提取文本，如纯装饰/分隔页）时，不存在可引用的原文
                # 片段；此时不得要求伪造证据，空 evidence 视为合法。
                page_has_text = bool(source_by_page[page]["text"].strip())
                if page_has_text and (not isinstance(evidence, list) or not evidence):
                    raise SemanticSceneError("SCENE.PAGE_EVIDENCE_MISSING",
                                             f"真实模型第 {page} 页无场景说明缺少原文证据。")
                if not isinstance(evidence, list):
                    evidence = []
                valid_ev = [str(x).strip() for x in evidence
                            if str(x).strip() and _excerpt_matches(source_by_page[page]["text"], str(x).strip())]
                if len(valid_ev) != len(evidence):
                    raise SemanticSceneError("SCENE.PAGE_EVIDENCE_NOT_GROUNDED",
                                             f"真实模型第 {page} 页的 no_scene 证据不在该页原文中。")
                evidence = valid_ev
            elif not str(reason or "").strip():
                reason = "本页为宏观概念论述或无具体实体场景"
            valid_ev = []
            for excerpt in evidence:
                if _excerpt_matches(source_by_page[page]["text"], str(excerpt)):
                    valid_ev.append(str(excerpt))
            if not valid_ev and not require_canonical_scene_id:
                phrases = [s.strip() for s in re.split(r"[，。！？\n]", source_by_page[page]["text"]) if len(s.strip()) >= 6]
                valid_ev = [phrases[0]] if phrases else ([source_by_page[page]["text"][:40]] if source_by_page[page]["text"] else [])
            evidence = valid_ev
        elif require_canonical_scene_id:
            if not isinstance(evidence, list) or not evidence:
                raise SemanticSceneError("SCENE.PAGE_EVIDENCE_MISSING",
                                         f"真实模型第 {page} 页的场景账本缺少原文证据。")
            if not all(str(x).strip() and _excerpt_matches(source_by_page[page]["text"], str(x).strip())
                       for x in evidence):
                raise SemanticSceneError("SCENE.PAGE_EVIDENCE_NOT_GROUNDED",
                                         f"真实模型第 {page} 页的场景账本证据不在该页原文中。")
        raw_coverage[page] = {"page": page, "scene_keys": list(dict.fromkeys(keys)),
                              "no_scene_reason": str(reason).strip() if reason else None,
                              "evidence": [str(x) for x in evidence]}
    if set(raw_coverage) != set(source_by_page):
        if require_canonical_scene_id:
            raise SemanticSceneError("SCENE.PAGE_COVERAGE_INCOMPLETE",
                                     "真实模型逐页场景账本必须覆盖原稿每一页，不能补造缺失页。")
        for page in sorted(set(source_by_page) - set(raw_coverage)):
            covered_keys = []
            ev_list = []
            for sc in scenes_list:
                k = sc.get("scene_key") or sc.get("canonical_scene_id")
                cands = sc.get("page_candidates") or []
                for cand in cands:
                    if cand.get("page") == page:
                        if k:
                            covered_keys.append(k)
                        ev = cand.get("evidence") or []
                        ev_list.extend(ev)
            src_text = source_by_page[page]["text"].strip()
            first_sent = [s.strip() for s in re.split(r"[，。！？\n]", src_text) if len(s.strip()) >= 6]
            raw_coverage[page] = {
                "page": page,
                "scene_keys": list(dict.fromkeys(covered_keys)),
                "no_scene_reason": None if covered_keys else "本页为宏观概念论述或无具体实体场景",
                "evidence": list(dict.fromkeys(ev_list)) if covered_keys else ([first_sent[0]] if first_sent else [])
            }

    raw_scenes = scenes_list
    if len(raw_scenes) > 160:
        raise SemanticSceneError("SCENE.TOO_MANY_SCENES", "整套演示文稿的场景数量超过安全上限。")
    scenes, key_to_canonical, signatures = [], {}, {}
    seen_keys = set()
    for item in raw_scenes:
        if not isinstance(item, dict):
            raise SemanticSceneError("SCENE.MODEL_SCHEMA_INVALID", "场景条目不是 JSON 对象。")
        key = str(item.get("scene_key") or "").strip()
        if not _KEY_RE.fullmatch(key):
            raise SemanticSceneError("SCENE.KEY_INVALID", "具体场景缺少稳定的小写 scene_key。")
        if key in seen_keys:
            raise SemanticSceneError("SCENE.KEY_DUPLICATED", f"场景键 {key} 被重复定义。")
        seen_keys.add(key)
        scene = {k: str(item.get(k) or "").strip()
                 for k in ("topic", "subject", "action", "setting", "label", "visual_query",
                           "lighting", "composition_camera", "style", "quality")}
        raw_depiction = item.get("depiction_type")
        if require_canonical_scene_id and not str(raw_depiction or "").strip():
            raise SemanticSceneError(
                "SCENE.DEPICTION_TYPE_MISSING",
                f"模型场景 {key} 必须明确声明 depiction_type='illustrative'。")
        requested_depiction = str(raw_depiction or "illustrative").strip().casefold()
        if requested_depiction != "illustrative":
            raise SemanticSceneError("SCENE.DEPICTION_TYPE_INVALID",
                                     "图库场景图必须明确标记为 illustrative，不能充当原文事件证据。")
        illustration_rationale = str(item.get("illustration_rationale") or "").strip()
        if require_canonical_scene_id and not illustration_rationale:
            raise SemanticSceneError("SCENE.ILLUSTRATION_RATIONALE_MISSING",
                                     f"场景 {key} 缺少原文语言的说明性配图关联理由。")
        if not illustration_rationale:
            illustration_rationale = "Illustrative scene linked to the cited source topic; not a factual event claim."
        if len(illustration_rationale) > 320 or "\n" in illustration_rationale or "\r" in illustration_rationale:
            raise SemanticSceneError("SCENE.ILLUSTRATION_RATIONALE_INVALID",
                                     f"场景 {key} 的说明性配图关联理由过长或含换行。")
        scene["depiction_type"] = "illustrative"
        scene["illustration_rationale"] = illustration_rationale
        scene["photo_search_terms"] = item.get("photo_search_terms")
        scene["environment"] = str(item.get("environment") or scene["setting"]).strip()
        scene["action_state"] = str(item.get("action_state") or scene["action"]).strip()
        if item.get("photo_search_terms") is not None:
            try:
                anchors = normalize_photo_search_terms(item.get("photo_search_terms"))
                scene["photo_search_terms"] = anchors
                scene["visual_query"] = build_provider_search_query_from_anchors(anchors)
                if not query_preserves_photo_search_anchors(scene["visual_query"], anchors):
                    raise ValueError("query does not preserve all anchors")
                scene["visual_queries"] = [scene["visual_query"]]
            except ValueError as exc:
                if require_canonical_scene_id:
                    raise SemanticSceneError(
                        "SCENE.PHOTO_SEARCH_ANCHORS_INVALID",
                        f"场景 {key} 的主体/动作对象/地点检索锚点无效。") from exc
                # Legacy deterministic fixtures may omit this newer hard-gate
                # structure; they stay on the bounded legacy matcher below.
                scene.pop("photo_search_terms", None)
        prompt_fields = ("subject", "environment", "action_state", "lighting",
                         "composition_camera", "style", "quality")
        missing_prompt_fields = [name for name in prompt_fields if not scene.get(name)]
        if require_canonical_scene_id and missing_prompt_fields:
            raise SemanticSceneError(
                "SCENE.IMAGE_PROMPT_SCHEMA_MISSING",
                f"场景 {key} 缺少图像提示字段：{', '.join(missing_prompt_fields)}。")
        scene["image_prompt_fields"] = {name: scene.get(name, "") for name in prompt_fields}
        if isinstance(item.get("image_plan"), dict):
            scene["image_plan"] = item["image_plan"]
        scene["scene_key"] = key
        raw_canonical = str(item.get("canonical_scene_id") or "").strip().casefold()
        canonical_scene_id = _normalize_canonical_scene_token(raw_canonical)
        if raw_canonical and not canonical_scene_id:
            # A key that normalizes to nothing is not an English stable key
            # (for example a Chinese phrase or punctuation only). Reject it
            # instead of silently inventing an identity.
            raise SemanticSceneError("SCENE.CANONICAL_SCENE_ID_INVALID",
                                     f"场景 {key} 的跨语言稳定身份键格式无效。")
        if not canonical_scene_id and require_canonical_scene_id:
            raise SemanticSceneError("SCENE.CANONICAL_SCENE_ID_MISSING",
                                     f"场景 {key} 缺少跨语言稳定身份键。")
        if not canonical_scene_id:
            # Deterministic callers/fixtures may omit the newer field; real model
            # plans require it so equivalent scenes in different languages merge.
            canonical_scene_id = key
        if not _KEY_RE.fullmatch(canonical_scene_id):
            raise SemanticSceneError("SCENE.CANONICAL_SCENE_ID_INVALID",
                                     f"场景 {key} 的跨语言稳定身份键格式无效。")
        scene["canonical_scene_id"] = canonical_scene_id
        alternatives = item.get("visual_query_alternatives") or []
        if not isinstance(alternatives, list) or any(not isinstance(x, str) for x in alternatives):
            raise SemanticSceneError("SCENE.QUERY_INVALID", f"场景 {key} 的备选照片查询格式无效。")
        if scene.get("photo_search_terms"):
            alternatives = [x for x in alternatives
                            if is_neutral_photo_search_query(str(x).strip())]
        queries = [scene["visual_query"], *[str(x).strip() for x in alternatives]]
        queries = list(dict.fromkeys(re.sub(r"\s+", " ", q).strip()[:180]
                                     for q in queries if q.strip()))
        if not all(scene[k] for k in ("subject", "action", "setting", "label")) or not queries:
            raise SemanticSceneError("SCENE.MODEL_SCHEMA_INVALID",
                                     f"场景 {key} 缺少主体、动作/关系、环境、标签或照片查询。")
        if len(queries) > 3 or any(not re.search(r"[A-Za-z]", q) for q in queries):
            raise SemanticSceneError("SCENE.QUERY_INVALID", f"场景 {key} 只能有最多三个中性英文查询。")
        scene["visual_query"] = queries[0]
        scene["visual_queries"] = queries
        scene["visual_query_alternatives"] = queries[1:]
        candidates = item.get("page_candidates")
        if (not require_canonical_scene_id
                and (not isinstance(candidates, list) or not candidates) and raw_coverage):
            reconstructed = []
            for row in raw_coverage.values():
                if (key in row.get("scene_keys", []) or
                        canonical_scene_id in row.get("scene_keys", [])):
                    ev = row.get("evidence") or []
                    reconstructed.append({"page": row["page"], "relevance": 0.9, "evidence": ev})
            if reconstructed:
                candidates = reconstructed
        if not isinstance(candidates, list) or not candidates:
            raise SemanticSceneError("SCENE.CANDIDATES_MISSING", f"场景 {key} 没有原文支持的候选页。")
        normalized_candidates = []
        candidate_pages = set()
        for candidate in candidates:
            try:
                page = int(candidate.get("page"))
                relevance = float(candidate.get("relevance", 0.5))
            except (TypeError, ValueError, AttributeError):
                raise SemanticSceneError("SCENE.CANDIDATE_INVALID", f"场景 {key} 的候选页无效。")
            if page not in source_by_page or page in candidate_pages or not 0 <= relevance <= 1:
                raise SemanticSceneError("SCENE.CANDIDATE_INVALID", f"场景 {key} 的候选页越界、重复或评分无效。")
            candidate_pages.add(page)
            excerpts = candidate.get("evidence") or []
            if not isinstance(excerpts, list) or not excerpts:
                raise SemanticSceneError("SCENE.EVIDENCE_MISSING", f"场景 {key} 的第 {page} 页缺少原文片段。")
            excerpts = [str(x).strip() for x in excerpts if str(x).strip()]
            if not excerpts or not all(_excerpt_matches(source_by_page[page]["text"], x) for x in excerpts):
                raise SemanticSceneError("SCENE.EVIDENCE_NOT_GROUNDED",
                                         f"场景 {key} 的证据不在第 {page} 页原文中。")
            normalized_candidates.append({"page": page, "relevance": relevance,
                                           "evidence": excerpts})
        scene["page_candidates"] = normalized_candidates
        scene["candidate_pages"] = sorted(candidate_pages)
        scene["_signature"] = _identity_signature(scene)
        if not scene["_signature"].replace("|", ""):
            raise SemanticSceneError("SCENE.IDENTITY_INVALID", f"场景 {key} 缺少可去重语义。")
        signature = scene["_signature"]
        localized_signature = "localized:" + _localized_identity_signature(scene)
        existing_signature = signatures.get(signature) or signatures.get(localized_signature)
        if existing_signature:
            canonical = existing_signature
            key_to_canonical[key] = canonical
            target = next(s for s in scenes if s["scene_key"] == canonical)
            if dedupe_audit is not None:
                dedupe_audit.append({
                    "decision": "merged",
                    "scene_key": key,
                    "into_scene_key": canonical,
                    "signature": signature,
                    "reason": "normalized canonical scene identity is identical",
                })
            merged = {c["page"]: c for c in target["page_candidates"]}
            for candidate in normalized_candidates:
                previous = merged.get(candidate["page"])
                if previous is None or candidate["relevance"] > previous["relevance"]:
                    merged[candidate["page"]] = candidate
                elif previous:
                    previous["evidence"] = list(dict.fromkeys(previous["evidence"] + candidate["evidence"]))
            target["page_candidates"] = list(merged.values())
            target["candidate_pages"] = sorted(merged)
        else:
            signatures[signature] = key
            signatures[localized_signature] = key
            key_to_canonical[key] = key
            scenes.append(scene)
            if dedupe_audit is not None:
                dedupe_audit.append({
                    "decision": "kept",
                    "scene_key": key,
                    "into_scene_key": None,
                    "signature": signature,
                    "reason": "first occurrence of this concrete scene",
                })

    for row in raw_coverage.values():
        remapped = [key_to_canonical.get(key, key) for key in row["scene_keys"]]
        row["scene_keys"] = list(dict.fromkeys(remapped))
        for key in row["scene_keys"]:
            scene = next((s for s in scenes if s["scene_key"] == key), None)
            if scene is None:
                if require_canonical_scene_id:
                    raise SemanticSceneError("SCENE.PAGE_SCENE_UNDEFINED",
                                             f"第 {row['page']} 页引用了 scenes 中未定义的场景键 {key}。")
                clean_key = re.sub(r"[^a-z0-9_]+", "_", str(key).lower()).strip("_") or f"scene_p{row['page']}"
                v_query = clean_key.replace("_", " ")
                ev = [e for e in (row.get("evidence") or []) if _excerpt_matches(source_by_page[row["page"]]["text"], str(e))]
                if not ev:
                    ev = [s.strip() for s in re.split(r"[，。！？\n]", source_by_page[row["page"]]["text"]) if len(s.strip()) >= 6][:1]
                scene = {
                    "scene_key": key,
                    "canonical_scene_id": clean_key,
                    "topic": source_by_page[row["page"]].get("title") or "业务场景",
                    "subject": v_query,
                    "action": "working in setting",
                    "setting": "indoor",
                    "label": v_query,
                    "visual_query": v_query,
                    "visual_query_alternatives": [v_query],
                    "page_candidates": [{"page": row["page"], "relevance": 0.85, "evidence": ev}],
                    "candidate_pages": [row["page"]],
                }
                scene["_signature"] = _identity_signature(scene)
                scenes.append(scene)
                key_to_canonical[key] = key
            elif row["page"] not in scene["candidate_pages"]:
                if require_canonical_scene_id:
                    raise SemanticSceneError("SCENE.PAGE_SCENE_LINK_INVALID",
                                             f"第 {row['page']} 页引用场景 {key}，但该页未显式列入 page_candidates。")
                ev = [e for e in (row.get("evidence") or []) if _excerpt_matches(source_by_page[row["page"]]["text"], str(e))]
                if not ev:
                    ev = [s.strip() for s in re.split(r"[，。！？\n]", source_by_page[row["page"]]["text"]) if len(s.strip()) >= 6][:1]
                scene["page_candidates"].append({"page": row["page"], "relevance": 0.85, "evidence": ev})
                scene["candidate_pages"] = sorted(scene["candidate_pages"] + [row["page"]])
    for scene in scenes if not require_canonical_scene_id else []:
        existing_cand_pages = {c["page"] for c in scene.get("page_candidates", [])}
        combined = " ".join([scene.get("topic", ""), scene.get("subject", ""), scene.get("action", "")])
        raw_tokens = re.split(r"[与和、/ \t\n,\(\)（）]+", combined)
        sub_terms = set()
        for tok in raw_tokens:
            tok = tok.strip()
            if len(tok) >= 2:
                sub_terms.add(tok)
            for i in range(len(tok) - 1):
                bi = tok[i:i+2]
                if len(bi) == 2:
                    sub_terms.add(bi)
        sorted_terms = sorted([t for t in sub_terms if len(t) >= 2], key=len, reverse=True)
        for p, p_data in source_by_page.items():
            if p in existing_cand_pages:
                continue
            for t in sorted_terms:
                if t in p_data["text"]:
                    for phrase in re.split(r"[，。！？\n]", p_data["text"]):
                        phrase = phrase.strip()
                        if len(phrase) >= 6 and t in phrase:
                            scene["page_candidates"].append({
                                "page": p,
                                "relevance": 0.85,
                                "evidence": [phrase]
                            })
                            scene["candidate_pages"].append(p)
                            break
                    break
        scene["candidate_pages"] = sorted(set(scene["candidate_pages"]))
    # A scene's page_candidates and the page-by-page coverage ledger are two
    # parallel lists the model must produce in the same response; it
    # occasionally lists a page under one but forgets to echo the scene_key
    # into the other. Every candidate here has already passed exact-excerpt
    # grounding against the source page above, so reconciling the ledger to
    # match is bookkeeping, not fabrication — it never adds a page or scene
    # that was not itself independently verified.
    for scene in scenes:
        for candidate in scene["page_candidates"]:
            row = raw_coverage[candidate["page"]]
            if scene["scene_key"] not in row["scene_keys"]:
                row["scene_keys"].append(scene["scene_key"])
                if row.get("no_scene_reason"):
                    row["no_scene_reason"] = None
    return scenes, [raw_coverage[p] for p in sorted(raw_coverage)]


def _rect_area(rect) -> float:
    return max(0.0, float(rect[2])) * max(0.0, float(rect[3]))


def _rect_intersection_area(a, b) -> float:
    return max(0.0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])) * \
           max(0.0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))


def _shape_rect(shape) -> tuple[float, float, float, float]:
    return tuple(float(v or 0) / _IN for v in
                 (shape.left, shape.top, shape.width, shape.height))


def _same_rect(a, b, tol: float = 0.003) -> bool:
    return all(abs(float(x) - float(y)) <= tol for x, y in zip(a, b))


def _picture_sha(shape) -> str | None:
    try:
        return _bytes_sha(shape.image.blob)
    except Exception:
        return None


def _is_background_shape(shape, slide_area: float, slide_height: float | None = None) -> bool:
    try:
        if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE:
            sr = _shape_rect(shape)
            area_ratio = _rect_area(sr) / max(0.001, slide_area)
            return area_ratio >= 0.85
        if getattr(shape, "has_table", False) or getattr(shape, "has_chart", False):
            return False
        if getattr(shape, "has_text_frame", False) and (shape.text_frame.text or "").strip():
            return False
        sr = _shape_rect(shape)
        area_ratio = _rect_area(sr) / max(0.001, slide_area)
        if area_ratio >= 0.85:
            return True
        if slide_height and area_ratio >= 0.40 and sr[1] <= 0.05 and sr[3] >= 0.90 * slide_height:
            return True
        return False
    except Exception:
        return False


def _visual_slots(visual_plan: dict | None, layout=None) -> dict[int, dict]:
    slots = {}
    if isinstance(visual_plan, dict):
        for key, item in (visual_plan.get("images") or {}).items():
            try:
                page = int(item.get("page") or key)
                rect = [float(x) for x in item.get("rect", [])]
                sha = str(item.get("sha256") or "")
            except (TypeError, ValueError):
                continue
            if page > 0 and len(rect) == 4 and sha:
                slots[page] = {"page": page, "rect": rect, "sha256": sha,
                               "file": item.get("file")}
    if layout is not None:
        for p_idx, slide in enumerate(layout.slides, 1):
            for shape in slide.shapes:
                if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE:
                    sha = _picture_sha(shape)
                    if not sha:
                        continue
                    shape_r = [round(float(v), 4) for v in _shape_rect(shape)]
                    if p_idx in slots and slots[p_idx].get("sha256") == sha:
                        slots[p_idx]["rect"] = shape_r
                    elif p_idx not in slots:
                        from . import visual_plan as vplan_mod
                        lib_shas = getattr(vplan_mod, "_LIBRARY_SHAS", None)
                        if lib_shas is None:
                            try:
                                lib_shas = {_sha(Path(v["file"])): k
                                            for k, v in vplan_mod.LIBRARY.items()
                                            if Path(v.get("file", "")).is_file()}
                                vplan_mod._LIBRARY_SHAS = lib_shas
                            except Exception:
                                lib_shas = {}
                        if sha in lib_shas:
                            slots[p_idx] = {"page": p_idx, "rect": shape_r, "sha256": sha,
                                           "file": vplan_mod.LIBRARY[lib_shas[sha]]["file"]}
    return slots


def _has_opaque_fill(shape) -> bool:
    try:
        fill = shape.fill
        if fill.type is None:
            return False
        transparency = fill.transparency
        return transparency is None or float(transparency) < 0.97
    except Exception:
        try:
            return shape._element.spPr.find("{http://schemas.openxmlformats.org/drawingml/2006/main}solidFill") is not None
        except Exception:
            return False


def _is_large_filled_shape(shape, slide_area: float) -> bool:
    try:
        if shape.shape_type == MSO_SHAPE_TYPE.LINE:
            return False
        if _rect_area(_shape_rect(shape)) / max(0.001, slide_area) < 0.025:
            return False
        if getattr(shape, "has_text_frame", False) and (shape.text_frame.text or "").strip():
            return False
        if getattr(shape, "has_table", False) or getattr(shape, "has_chart", False):
            return True
        return _has_opaque_fill(shape)
    except Exception:
        return False


def _text_overlap(rect, slide) -> tuple[float, bool]:
    area = max(0.001, _rect_area(rect))
    hit = 0.0
    opaque_text_hit = False
    for shape in slide.shapes:
        if not getattr(shape, "has_text_frame", False):
            continue
        text = (shape.text_frame.text or "").strip()
        if not text:
            continue
        overlap = _rect_intersection_area(rect, _shape_rect(shape))
        hit += overlap
        if overlap / area > 0.01 and _has_opaque_fill(shape):
            opaque_text_hit = True
    return min(1.0, hit / area), opaque_text_hit


def _can_reflow_shape(shape, rect, pw: float, ph: float, slide_area: float,
                      shape_index: int | None = None, slide=None) -> dict | None:
    if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE:
        return None
    if getattr(shape, "has_table", False) or getattr(shape, "has_chart", False):
        return None
    if not getattr(shape, "has_text_frame", False):
        return None
    text = (shape.text_frame.text or "").strip()
    if not text:
        return None
    if _has_opaque_fill(shape) or _is_large_filled_shape(shape, slide_area):
        return None
    sr = _shape_rect(shape)
    if sr[0] < rect[0] and rect[0] >= 0.45 * pw:
        avail_w = rect[0] - sr[0] - 0.25
        if avail_w < 4.2:
            return None
        paras = [p.text.strip() for p in shape.text_frame.paragraphs if p.text.strip()]
        if not paras or len(paras) > 10:
            return None
        chars_per_line = max(14, int(avail_w * 3.8))
        total_lines = sum(max(1, int(round(len(p) / float(chars_per_line) + 0.4))) for p in paras)
        est_h = total_lines * 0.38 + len(paras) * 0.10
        bottom_max = ph - 0.18
        # Anchor to the shape's own original top, not a fixed page-wide band:
        # a hardcoded top_min meant every reflowed shape on a page landed in
        # the same slot, so a second overlapping text block (title, then body,
        # then a column label — the normal stacked-block layout of this theme)
        # could never be reflowed too, since it would stack on top of the
        # first instead of keeping its own place in the column. Each shape
        # staying at its own top never collides with a sibling that already
        # didn't overlap it before narrowing (2026-10-27 job-c915c8c7deff:
        # 0/7 scenes placed — every candidate rect had 2+ stacked text blocks
        # and only the first could ever be reflowed).
        top_min = sr[1]
        # Stretching the reflowed shape's own box to "bottom_max" assumes
        # that whole span is empty. It is only checked for overlap against
        # the *photo*'s rect above — never against some other shape (e.g.
        # a page-bottom callout) that was never in the photo's footprint
        # and so never tripped the blocking-overlap check, yet now sits
        # inside the stretched box once this shape claims the full column
        # (2026-10-27 job-c0af57f36099 P9: a one-line title reflowed this
        # way swallowed the space a conclusion textbox below it needed).
        # Cap the stretch at whatever such a shape's own top actually is.
        if slide is not None:
            for other in slide.shapes:
                # `is` never matches here: python-pptx hands back a fresh
                # wrapper object on each iteration/index, even for the same
                # underlying XML element, so identity comparison silently
                # treats this shape as its own "neighbor" below itself —
                # shape_id is the element's stable identity, not the
                # wrapper's.
                if other.shape_id == shape.shape_id:
                    continue
                o_l, o_t, o_w, o_h = _shape_rect(other)
                if o_t <= top_min + 0.02:
                    continue  # not below the reflow start — not a floor
                if min(sr[0] + avail_w, o_l + o_w) <= max(sr[0], o_l) + 0.02:
                    continue  # different column
                if (_rect_intersection_area(sr, (o_l, o_t, o_w, o_h))
                        / max(0.001, _rect_area((o_l, o_t, o_w, o_h))) > 0.5):
                    # A decorative backdrop chip sitting directly behind this
                    # very shape's own text (near-identical rect, opaque fill,
                    # no text of its own) is not a sibling block "below" it —
                    # it moves with the shape, it is not a floor the reflowed
                    # box must stop above (2026-10-27 job-c915c8c7deff P1:
                    # "Shape 5", the colored pill behind the "当下热点切入"
                    # label, capped the label's own reflow to a negative span).
                    continue
                bottom_max = min(bottom_max, o_t - 0.16)
        if est_h > bottom_max - top_min:
            return None
        new_top = round(top_min, 4)
        new_h = round(bottom_max - new_top, 4)
        return {"name": shape.name, "shape_index": shape_index,
                "width": round(avail_w, 4), "top": new_top, "height": new_h}
    return None


def _estimated_text_height_for_row(shape, *, width: float, line_spacing: float = 1.05,
                                   space_after_pt: float = 4.0) -> dict | None:
    """Conservative fit estimate for preserving a wide text block above two photos."""
    frame = getattr(shape, "text_frame", None)
    if frame is None or not (frame.text or "").strip():
        return None
    # python-pptx stores margins in EMU; the supplied width is in inches.
    available_width_pt = (float(width) * 72.0 -
                          (float(frame.margin_left or 0) + float(frame.margin_right or 0)) / 12700.0)
    if available_width_pt <= 0:
        return None
    lines = 0
    max_font_pt = 0.0
    paragraphs = 0
    for para in frame.paragraphs:
        if not (para.text or "").strip():
            continue
        paragraphs += 1
        run_width_pt = 0.0
        para_font_pt = 0.0
        for run in para.runs:
            text = str(run.text or "")
            if not text:
                continue
            size = run.font.size
            if size is None:
                # Do not guess inherited sizes when deciding whether text can
                # share a slide with a new photo row.
                return None
            font_pt = float(size.pt)
            para_font_pt = max(para_font_pt, font_pt)
            for char in text:
                if char.isspace():
                    units = 0.28
                elif unicodedata.east_asian_width(char) in {"F", "W"}:
                    units = 1.0
                elif unicodedata.category(char).startswith("P"):
                    units = 0.5
                else:
                    units = 0.56
                run_width_pt += font_pt * units
        if para_font_pt <= 0:
            return None
        lines += max(1, int(math.ceil(run_width_pt / available_width_pt)))
        max_font_pt = max(max_font_pt, para_font_pt)
    if not paragraphs or max_font_pt <= 0:
        return None
    top_bottom_pt = (float(frame.margin_top or 0) + float(frame.margin_bottom or 0)) / 12700.0
    estimate_pt = lines * max_font_pt * line_spacing + paragraphs * space_after_pt + top_bottom_pt
    return {"paragraph_count": paragraphs, "estimated_lines": lines,
            "estimated_height_in": round(estimate_pt / 72.0, 4),
            "max_font_pt": round(max_font_pt, 3)}


def _can_reflow_shape_for_photo_row(shape, *, photo_top: float,
                                    photo_height: float, ph: float,
                                    shape_index: int | None = None) -> dict | None:
    if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE:
        return None
    if getattr(shape, "has_table", False) or getattr(shape, "has_chart", False):
        return None
    if not getattr(shape, "has_text_frame", False):
        return None
    text = (shape.text_frame.text or "").strip()
    if not text or _has_opaque_fill(shape):
        return None
    x, y, width, height = _shape_rect(shape)
    if x < 0.3 or width < 8.0 or y < 1.45:
        return None
    scale = ph / 7.5
    new_top = 1.76 * scale
    gap = 0.16 * scale
    new_height = round(photo_top - gap - new_top, 4)
    if new_height <= 0:
        return None
    spacing = 1.05
    after_pt = 4.0
    estimate = _estimated_text_height_for_row(
        shape, width=width, line_spacing=spacing, space_after_pt=after_pt)
    if estimate is None or estimate["estimated_height_in"] > new_height - 0.04:
        return None
    return {"name": shape.name, "shape_index": shape_index,
            "width": round(width, 4),
            "left": round(x, 4), "top": new_top, "height": new_height,
            "line_spacing": spacing, "space_after_pt": after_pt,
            "text_fit": estimate}


def _placement_candidates(pw: float, ph: float, page_count: int,
                          slot: dict | None, *, paired_photo_row: bool = False) -> list[dict]:
    out = []
    if paired_photo_row:
        if not 1.6 <= pw / max(0.001, ph) <= 1.9:
            return []
        y, h = ph * (4.42 / 7.5), ph * (2.88 / 7.5)
        w = pw * (5.95 / 13.333)
        if page_count == 0:
            rect = (pw * (0.45 / 13.333), y, w, h)
        elif page_count == 1:
            rect = (pw * (6.85 / 13.333), y, w, h)
        else:
            return []
        return [{"rect": [round(float(x), 4) for x in rect],
                 "mode": "paired_photo_row", "replaced_visual_sha256": None}]
    if page_count == 0 and slot:
        out.append({"rect": list(slot["rect"]), "mode": "replace_visual_image",
                    "replaced_visual_sha256": slot["sha256"]})
    if page_count == 0:
        rects = [
            (pw * .54, ph * .24, pw * .42, ph * .68),
            (pw * .04, ph * .24, pw * .42, ph * .68),
            (pw * .54, ph * .20, pw * .42, ph * .72),
            (pw * .04, ph * .20, pw * .42, ph * .72),
            (pw * .555, ph * .055, pw * .415, ph * .89),
            (pw * .03, ph * .12, pw * .42, ph * .78),
            (pw * .225, ph * .45, pw * .55, ph * .50),
            (pw * .255, ph * .57, pw * .49, ph * .40),
            (pw * .26, ph * .62, pw * .48, ph * .35),
            (pw * .52, ph * .18, pw * .455, ph * .48),
        ]
    else:
        rects = [
            (pw * .04, ph * .20, pw * .42, ph * .72),
            (pw * .54, ph * .20, pw * .42, ph * .72),
            (pw * .04, ph * .57, pw * .44, ph * .40),
            (pw * .52, ph * .57, pw * .44, ph * .40),
            (pw * .02, ph * .62, pw * .46, ph * .35),
            (pw * .52, ph * .62, pw * .46, ph * .35),
        ]
    out.extend({"rect": [round(float(x), 4) for x in r], "mode": "empty_area",
                "replaced_visual_sha256": None} for r in rects)
    return out


def _contain_rect(rect, image_aspect: float) -> list[float]:
    """Fit the complete scene photo inside a safe area without cropping it."""
    left, top, width, height = [float(x) for x in rect]
    if image_aspect <= 0:
        return [round(x, 4) for x in (left, top, width, height)]
    box_aspect = width / height
    if image_aspect >= box_aspect:
        fitted_height = width / image_aspect
        top += (height - fitted_height) / 2
        height = fitted_height
    else:
        fitted_width = height * image_aspect
        left += (width - fitted_width) / 2
        width = fitted_width
    return [round(float(x), 4) for x in (left, top, width, height)]


def _replacement_aspect_limit() -> float:
    """Read the frozen HG13 replacement-slot crop limit from its policy."""
    policy = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
    value = float(policy["rules"]["replacement_slot_max_image_to_box_aspect_factor"])
    if not math.isfinite(value) or value < 1.0:
        raise ValueError("invalid replacement-slot aspect factor in HG13 policy")
    return value


def _image_to_box_aspect_factor(image_aspect: float, box_aspect: float) -> float | None:
    """Return the symmetric source-to-target ratio, or None for invalid geometry."""
    if (not math.isfinite(image_aspect) or not math.isfinite(box_aspect)
            or image_aspect <= 0 or box_aspect <= 0):
        return None
    return max(image_aspect / box_aspect, box_aspect / image_aspect)


def _cover_crop_matches_geometry(image_aspect: float, box_aspect: float,
                                 crop: list[float] | tuple[float, ...]) -> bool:
    """Check OOXML crop fractions against the amount of crop needed for cover."""
    if len(crop) != 4:
        return False
    try:
        left, top, right, bottom = [float(value) for value in crop]
    except (TypeError, ValueError):
        return False
    values = (left, top, right, bottom)
    if (any(not math.isfinite(value) or value < -0.0001 or value >= 1 for value in values)
            or left + right >= 1 or top + bottom >= 1):
        return False
    if image_aspect > box_aspect:
        expected_horizontal = 1.0 - box_aspect / image_aspect
        return (abs((left + right) - expected_horizontal) <= _COVER_CROP_TOLERANCE
                and top + bottom <= _COVER_CROP_TOLERANCE)
    expected_vertical = 1.0 - image_aspect / box_aspect
    return (abs((top + bottom) - expected_vertical) <= _COVER_CROP_TOLERANCE
            and left + right <= _COVER_CROP_TOLERANCE)


def _placement_quality(slide, rect, mode: str, slot: dict | None,
                       assigned_rects: list[list[float]], slide_area: float,
                       pw: float | None = None, ph: float | None = None) -> dict:
    area_ratio = _rect_area(rect) / max(0.001, slide_area)
    effective_text_overlap = 0.0
    opaque_text = False
    area = max(0.001, _rect_area(rect))
    blockers = []
    reflow_shapes = []
    for shape_index, shape in enumerate(slide.shapes):
        shape_rect = _shape_rect(shape)
        overlap = _rect_intersection_area(rect, shape_rect)
        if overlap <= 0:
            continue
        if _is_background_shape(shape, slide_area, slide_height=ph):
            # A full-slide or full-height half-slide color panel sits behind
            # everything in z-order (it is always among the first shapes in
            # document order); a newly inserted photo is appended later and
            # so is drawn on top of it. Only replace_visual_image skipped it
            # before, so every empty_area candidate on a themed deck with a
            # backdrop panel (the normal case for this theme) was blocked by
            # "filled_shape_or_card" regardless of whether anything was
            # actually in front of the photo (2026-10-27 job-c915c8c7deff:
            # 0/7 scenes placed, all unplaced for SCENE.NO_SAFE_PHOTO_AREA).
            continue
        if mode == "replace_visual_image" and slot:
            if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE \
                    and _picture_sha(shape) == slot["sha256"]:
                continue
            has_text = getattr(shape, "has_text_frame", False) and (shape.text_frame.text or "").strip()
            if not has_text and (_same_rect(shape_rect, slot["rect"]) or _same_rect(shape_rect, rect)
                                 or (_rect_intersection_area(shape_rect, slot["rect"]) / max(0.001, _rect_area(shape_rect)) > 0.85)):
                # A mask belonging to the replaced generic image
                continue
        if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE:
            blockers.append("existing_picture")
        elif getattr(shape, "has_table", False) or getattr(shape, "has_chart", False):
            blockers.append("table_or_chart")
        elif _is_large_filled_shape(shape, slide_area):
            blockers.append("filled_shape_or_card")
        elif getattr(shape, "has_text_frame", False) and (shape.text_frame.text or "").strip():
            if _has_opaque_fill(shape):
                opaque_text = True
                effective_text_overlap += overlap
            elif pw and ph and mode == "paired_photo_row":
                reflow = _can_reflow_shape_for_photo_row(shape, photo_top=rect[1],
                                                        photo_height=rect[3], ph=ph,
                                                        shape_index=shape_index)
                if reflow is not None:
                    reflow_shapes.append(reflow)
                else:
                    effective_text_overlap += overlap
            elif pw and ph and mode == "empty_area":
                # _can_reflow_shape anchors each shape to its own original
                # top/height (not a shared fixed band), so multiple stacked
                # text blocks that overlap the same photo rect can each
                # reflow independently without landing on top of one
                # another — they didn't collide vertically before narrowing,
                # and narrowing only shrinks their width.
                reflow = _can_reflow_shape(shape, rect, pw, ph, slide_area,
                                           shape_index=shape_index, slide=slide)
                if reflow is not None:
                    reflow_shapes.append(reflow)
                else:
                    effective_text_overlap += overlap
            else:
                effective_text_overlap += overlap
    for old in assigned_rects:
        if _rect_intersection_area(rect, old) / max(0.001, _rect_area(rect)) > 0.02:
            blockers.append("another_scene_photo")
    if area_ratio < _MIN_AREA_RATIO:
        blockers.append("photo_area_too_small")
    text_overlap_ratio = min(1.0, effective_text_overlap / area)
    if mode == "replace_visual_image":
        if text_overlap_ratio > _MAX_TEXT_OVERLAP or opaque_text:
            blockers.append("text_or_opaque_card_overlap")
    elif text_overlap_ratio > _MAX_EMPTY_AREA_TEXT_OVERLAP:
        blockers.append("text_overlap")
    return {"safe": not blockers, "area_ratio": area_ratio,
            "text_overlap_ratio": text_overlap_ratio, "opaque_text_overlap": opaque_text,
            "blockers": sorted(set(blockers)), "reflow_shapes": reflow_shapes}


def _choose_assignment(scene: dict, pages: list[dict], prs, page_counts: Counter,
                       occupied: dict[int, list[list[float]]], slots: dict[int, dict],
                       image_aspect: float | None = None,
                       paired_photo_pages: set[int] | None = None) -> dict | None:
    pw, ph = prs.slide_width / _IN, prs.slide_height / _IN
    slide_area = pw * ph
    max_replacement_aspect_factor = _replacement_aspect_limit()
    candidates = sorted(scene["page_candidates"],
                        key=lambda c: (page_counts[c["page"]], -c["relevance"], c["page"]))
    safe_placements = []
    paired_photo_pages = set(paired_photo_pages or ())
    for candidate in candidates:
        page = candidate["page"]
        if page_counts[page] >= _MAX_SCENES_PER_PAGE:
            continue
        slide = prs.slides[page - 1]
        slot = slots.get(page) if page_counts[page] == 0 else None
        for placement in _placement_candidates(
                pw, ph, page_counts[page], slot,
                paired_photo_row=page in paired_photo_pages):
            target_rect = placement["rect"]
            mode = placement["mode"]
            if mode == "replace_visual_image" and image_aspect is not None:
                box_aspect = float(target_rect[2]) / max(0.001, float(target_rect[3]))
                factor = _image_to_box_aspect_factor(float(image_aspect), box_aspect)
                if factor is None or factor > max_replacement_aspect_factor + 0.0001:
                    # An unsafe generic-image slot must not force a drastic crop;
                    # the planner can try a safe empty area or leave the scene unresolved.
                    continue
            rect = _contain_rect(target_rect, image_aspect) if (
                image_aspect and mode in {"empty_area", "paired_photo_row"}) else target_rect
            quality = _placement_quality(slide, rect, placement["mode"], slot,
                                         occupied.get(page, []), slide_area,
                                         pw=pw, ph=ph)
            if not quality["safe"]:
                continue
            result = {"page": page, "rect": rect, "target_rect": target_rect,
                      "mode": mode,
                      "fit_mode": "contain" if mode in {"empty_area", "paired_photo_row"} else "cover",
                      "replaced_visual_sha256": placement.get("replaced_visual_sha256"),
                      "quality": quality, "relevance": candidate["relevance"],
                      "evidence": candidate["evidence"],
                      "reflow_shapes": quality.get("reflow_shapes", [])}
            # A genuine planned image slot stays preferred. For empty areas,
            # use the largest uncropped photo area among safe candidates.
            if mode == "replace_visual_image":
                return result
            safe_placements.append(result)
    if not safe_placements:
        return None
    return max(safe_placements,
               key=lambda item: (item["quality"]["area_ratio"],
                                 item["relevance"], -item["page"]))


# 版位容量类原因：稿面放不下，没有任何东西被伪造或替代。
# 这类原因可以如实登记为"未放置"，不需要让整份交付作废。
# 其余原因（许可、复核、重复、尺寸、安全元数据）关系到真实性与合规，
# 仍然必须硬阻断——绝不以文字卡片或未经核验的图片替代场景图。
#: 说明卡片兜底开启时，"找不到经复核的照片"的场景改记为这个原因并按未放置处理。
NO_VERIFIED_PHOTO_SKIPPED = "SCENE.NO_VERIFIED_PHOTO_SKIPPED"

CAPACITY_UNRESOLVED_REASONS = frozenset({
    "SCENE.NO_SAFE_PHOTO_AREA",
    "SCENE.PAGE_SCENE_CAPACITY_EXCEEDED",
    NO_VERIFIED_PHOTO_SKIPPED,
})

#: "没有一张候选图可用"类原因：检索为空、图库网络失败、每张候选都被许可/
#: 格式/分辨率/描述/安全/看图否决、生图不可用或不合格。跳过这类场景**不会
#: 插入任何图**，所以是安全的。仍然阻断的是计划本身的问题或复核失灵：
#: 查询/锚点无效、隐私检查、看图服务不可用（VISUAL_REVIEW_UNAVAILABLE /
#: VISION_*）、图像库缺失等——这些不是"没找到"，而是"没法正确判断"。
NO_PHOTO_FOUND_REASONS = frozenset({
    # retrieval / transport
    "SCENE.NO_LICENSED_PHOTO", "SCENE.PROVIDER_ROWS_INELIGIBLE",
    "SCENE.SEARCH_BUDGET_EXHAUSTED", "SCENE.PROVIDER_UNAVAILABLE",
    "SCENE.PROVIDER_TLS_HANDSHAKE_TIMEOUT", "SCENE.PROVIDER_HTTP_ERROR",
    "SCENE.PROVIDER_RATE_LIMITED", "SCENE.PROVIDER_AUTH_FAILED",
    "SCENE.PROVIDER_RESPONSE_TOO_LARGE", "SCENE.PEXELS_API_KEY_MISSING",
    "SCENE.NO_IMAGE_PROVIDER",
    # every candidate rejected (nothing gets inserted)
    "SCENE.IMAGE_CONTEXT_UNVERIFIED", "SCENE.IMAGE_RELEVANCE_TOO_WEAK",
    "SCENE.PHOTO_STYLE_MISMATCH", "SCENE.PHOTO_NOT_PHOTOGRAPHIC",
    "SCENE.PHOTO_ASPECT_UNSUITABLE", "SCENE.PHOTO_CONTRAST_INSUFFICIENT",
    "SCENE.LICENSE_NOT_ALLOWED", "SCENE.CONTENT_SAFETY_METADATA_BLOCK",
    "SCENE.VISUAL_CONTENT_MISMATCH", "SCENE.VISUAL_REVIEW_CANDIDATE_LIMIT",
    "SCENE.IMAGE_RESOLUTION_TOO_LOW", "SCENE.IMAGE_FORMAT_UNSUPPORTED",
    "SCENE.IMAGE_INVALID", "SCENE.IMAGE_TOO_LARGE", "SCENE.IMAGE_EMPTY",
    "SCENE.IMAGE_FETCH_FAILED", "SCENE.IMAGE_TLS_HANDSHAKE_TIMEOUT",
    "SCENE.IMAGE_RATE_LIMITED", "SCENE.IMAGE_URL_INVALID",
    "SCENE.IMAGE_REDIRECT_BLOCKED", "SCENE.PHOTO_URL_NOT_PUBLIC_HTTPS",
    "SCENE.SOURCE_PAGE_NOT_PUBLIC_HTTPS",
    # generated-image fallback unavailable or rejected
    "SCENE.GEN_IMAGE_UNAVAILABLE", "SCENE.GEN_IMAGE_FAILED",
    "SCENE.GEN_IMAGE_ASPECT_UNSUITABLE", "SCENE.GEN_IMAGE_NOT_PHOTOGRAPHIC",
    "SCENE.GEN_IMAGE_RESOLUTION_TOO_LOW", "SCENE.GEN_IMAGE_FORMAT_UNSUPPORTED",
    "SCENE.GEN_IMAGE_DECODE_FAILED", "SCENE.GEN_IMAGE_DUPLICATE",
    "SCENE.GEN_IMAGE_NEAR_DUPLICATE",
})

#: 上游"没回图"类失败在重试前短暂退避；语义判否（主体/动作/环境不可见）不退避。
_GEN_IMAGE_BACKOFF_CODES = frozenset({"SCENE.GEN_IMAGE_FAILED"})

#: WENGAO_SCENE|<scene_key>|<sha256> 的允许长度：场景键上限 79 字符 + 64 位哈希 + 分隔符。
_MAX_SCENE_MARKER_LEN = 13 + 79 + 1 + 64


def split_unresolved_scenes(unresolved) -> tuple[list, list]:
    """Split unresolved scenes into (blocking, capacity_only).

    A scene left out because the deck has no room is reported, not fatal.
    Anything touching provenance, licensing, review or duplication blocks.
    """
    blocking, capacity = [], []
    for item in unresolved or []:
        reason = (item or {}).get("reason")
        (capacity if reason in CAPACITY_UNRESOLVED_REASONS else blocking).append(item)
    return blocking, capacity


def _plan_hash(plan: dict) -> str:
    return _canonical_sha({k: v for k, v in plan.items() if k != "plan_sha256"})


def _audited_plan_view_drift(audited_view: dict, plan: dict) -> list[str]:
    """List where the frozen audited view departs from the final plan.

    The only accepted difference is the deterministic canonical_scene_id
    normalization applied after the audit; anything else is a real drift.
    """
    final_view = _coverage_plan_view(plan)
    audited = _coverage_plan_view(audited_view)  # fresh dicts; safe to rewrite
    for scene in audited["scenes"]:
        raw = str(scene.get("canonical_scene_id") or "").strip().casefold()
        scene["canonical_scene_id"] = _normalize_canonical_scene_token(raw) or raw
    drift = []
    if audited["page_coverage"] != final_view["page_coverage"]:
        drift.append("page_coverage")
    if len(audited["scenes"]) != len(final_view["scenes"]):
        drift.append("scenes")
    else:
        for a, b in zip(audited["scenes"], final_view["scenes"]):
            drift.extend(f"{a.get('scene_key')}.{k}" for k in a if a[k] != b.get(k))
    return drift


def _privacy_query_check(scene: dict, source_pages: list[dict]) -> None:
    raw_queries = scene.get("visual_queries") or [scene.get("visual_query")]
    for raw_query in raw_queries:
        raw_query = str(raw_query or "")
        if re.search(r"\d|[\u3400-\u9fff]", raw_query):
            raise SemanticSceneError("SCENE.QUERY_CONTAINS_SOURCE_TEXT",
                                     f"场景 {scene.get('scene_key')} 的远程照片查询含数字或非英文源文字符。")
        query = _norm(raw_query)
        for candidate in scene["page_candidates"]:
            for excerpt in candidate["evidence"]:
                evidence = _norm(excerpt)
                if len(evidence) >= 16 and (evidence in query or query in evidence):
                    raise SemanticSceneError("SCENE.QUERY_CONTAINS_SOURCE_TEXT",
                                             f"场景 {scene['scene_key']} 的远程查询疑似包含原文片段。")
        for page in source_pages:
            text = _norm(page["text"])
            if len(query) >= 28 and query in text:
                raise SemanticSceneError("SCENE.QUERY_CONTAINS_SOURCE_TEXT",
                                         f"场景 {scene['scene_key']} 的照片查询与原文长句重合。")


_EVIDENCE_NOT_GROUNDED_RE = re.compile(r"^场景 (.+) 的证据不在第 (\d+) 页原文中。$")


def _repair_scene_evidence_grounding(semantic_resolution: dict, pages: list[dict],
                                     exc: SemanticSceneError, *, model, call) -> bool:
    """One bounded, same-model repair for a scene whose cited evidence is not a
    literal excerpt of its claimed page.

    Mirrors the other same-model, source-scoped repair passes in scene_model.py
    (gap repair, anchor repair): it gives the model one more source-grounded
    chance to quote correctly, but never accepts or invents anything that fails
    the exact-substring check. Mutates ``semantic_resolution`` in place and
    returns True if a repair was attempted (whether or not it produced a
    grounded excerpt); returns False if the error could not be targeted at all.
    """
    match = _EVIDENCE_NOT_GROUNDED_RE.match(exc.detail or "")
    if not match:
        return False
    scene_key, page_no = match.group(1), int(match.group(2))
    source_by_page = {int(p["page"]): p for p in pages}
    page_text = source_by_page.get(page_no, {}).get("text", "")
    if not page_text:
        return False
    scenes_list = semantic_resolution.get("scenes")
    if not isinstance(scenes_list, list):
        return False
    scene_item = next((s for s in scenes_list if isinstance(s, dict)
                       and str(s.get("scene_key") or "") == scene_key), None)
    if scene_item is None:
        return False
    candidates = scene_item.get("page_candidates")
    if not isinstance(candidates, list):
        return False
    candidate = next((c for c in candidates if isinstance(c, dict)
                      and int(c.get("page", -1)) == page_no), None)
    if candidate is None:
        return False
    repair_prompt = (
        "一次性证据重新定位：下面给出第 " + str(page_no) + " 页的完整原文，以及一个具体场景的"
        "主体/动作/环境描述。该场景之前给出的证据片段经核实并不是该页原文中的连续片段。"
        "现在只允许你从下面这段原文中逐字复制一个连续片段作为证据（8-60 字，不得改写、"
        "翻译、总结或添加任何原文之外的文字）；如果这页原文确实不能支持这个场景，"
        "按 schema 明确返回空证据，不得编造。只返回 JSON，不要输出 Markdown 或解释。\n"
        f"场景主体：{scene_item.get('subject', '')}；动作：{scene_item.get('action', '')}；"
        f"环境：{scene_item.get('setting', '')}；标签：{scene_item.get('label', '')}。\n"
        f"第 {page_no} 页原文：\n{page_text}\n"
        "严格只返回：{\"evidence\":[\"...逐字复制的连续原文片段...\"]} 或 {\"evidence\":[]}。"
    )
    try:
        payload, _meta, _raw = _call_json_payload_once_on_format_error(
            model, repair_prompt, call,
            phase=f"证据重新定位（场景 {scene_key} 第 {page_no} 页）")
    except SceneModelError:
        return True
    new_evidence = payload.get("evidence")
    if not isinstance(new_evidence, list):
        return True
    valid_evidence = [str(x).strip() for x in new_evidence if str(x).strip()
                      and _excerpt_matches(page_text, str(x).strip())]
    if valid_evidence:
        candidate["evidence"] = valid_evidence
        return True
    # Repair could not ground this page either; drop only this one candidate
    # page rather than inventing evidence.
    candidates.remove(candidate)
    if candidates:
        return True
    # That was the scene's only candidate: nothing about it can be
    # substantiated anywhere in the source, so it cannot be kept at all
    # (SCENE.CANDIDATES_MISSING would otherwise hard-fail the whole deck on
    # the very next validation pass over a scene that is, at this point,
    # indistinguishable from one that was never proposed). Drop the scene
    # and fall any page_coverage row that only pointed at it back to an
    # explicit, audited no_scene_reason, exactly like a page the model
    # itself decided had no supportable scene.
    scenes_list.remove(scene_item)
    raw = semantic_resolution
    for k in ("scene_plan", "plan", "data", "result", "output", "semantic_scenes"):
        if isinstance(raw.get(k), dict) and any(sk in raw[k] for sk in ("scenes", "scene", "page_coverage")):
            raw = raw[k]
            break
    cov_list = raw.get("page_coverage")
    if isinstance(cov_list, list):
        for row in cov_list:
            if not isinstance(row, dict):
                continue
            keys = row.get("scene_keys")
            if not isinstance(keys, list) or scene_key not in keys:
                continue
            row["scene_keys"] = [k for k in keys if k != scene_key]
            if not row["scene_keys"]:
                row["no_scene_reason"] = (
                    row.get("no_scene_reason")
                    or f"场景 {scene_key} 的证据重新定位两次均未能在原文中找到可核验的连续片段，"
                       "按规则移除该场景，本页改记无场景原因。")
    return True


_PAGE_EVIDENCE_NOT_GROUNDED_RE = re.compile(
    r"真实模型第\s*(\d+)\s*页的(?:场景账本|no_scene)证据不在该页原文中")


def _repair_page_coverage_evidence(semantic_resolution: dict, pages: list[dict],
                                   exc: SemanticSceneError, *, model, call) -> bool:
    """One bounded, same-model repair for a page_coverage row whose cited
    evidence is not a literal excerpt of its claimed page.

    This is the page_coverage ledger's own evidence field — a distinct piece
    of data from any scene's page_candidates evidence, which
    _repair_scene_evidence_grounding already covers. Both failure modes share
    the same root cause (the model paraphrasing instead of quoting verbatim),
    but only the scene-level one previously had a repair path; a page-coverage
    mismatch hard-failed the whole deck with zero retry, even for a page whose
    scattered dashboard/table text makes an exact contiguous quote genuinely
    hard to produce on the first try.
    """
    match = _PAGE_EVIDENCE_NOT_GROUNDED_RE.search(exc.detail or "")
    if not match:
        return False
    page_no = int(match.group(1))
    source_by_page = {int(p["page"]): p for p in pages}
    page_text = source_by_page.get(page_no, {}).get("text", "")
    if not page_text:
        return False
    raw = semantic_resolution
    for k in ("scene_plan", "plan", "data", "result", "output", "semantic_scenes"):
        if isinstance(raw.get(k), dict) and any(sk in raw[k] for sk in ("scenes", "scene", "page_coverage")):
            raw = raw[k]
            break
    cov_list = raw.get("page_coverage")
    if not isinstance(cov_list, list):
        return False
    row = next((r for r in cov_list if isinstance(r, dict)
               and not isinstance(r.get("page"), bool)
               and isinstance(r.get("page"), (int, str))
               and str(r.get("page")).strip().lstrip("-").isdigit()
               and int(r.get("page")) == page_no), None)
    if row is None:
        return False
    keys = row.get("scene_keys") or row.get("scene_key") or []
    reason = row.get("no_scene_reason")
    repair_prompt = (
        "一次性证据重新定位：下面给出第 " + str(page_no) + " 页的完整原文，以及该页在逐页场景账本中"
        "已登记的场景键/无场景原因。该页之前给出的证据片段经核实并不是该页原文中的连续片段。"
        "现在只允许你从下面这段原文中逐字复制一个或多个连续片段作为证据（每条 4-60 字，不得改写、"
        "翻译、总结、拼接跨行内容或添加任何原文之外的文字）；如果这页原文确实不能支持已登记的场景/"
        "原因，按 schema 明确返回空证据，不得编造。只返回 JSON，不要输出 Markdown 或解释。\n"
        f"该页已登记场景键：{keys or '（无，已登记 no_scene_reason）'}；"
        f"no_scene_reason：{reason or '（无）'}。\n"
        f"第 {page_no} 页原文：\n{page_text}\n"
        "严格只返回：{\"evidence\":[\"...逐字复制的连续原文片段...\"]} 或 {\"evidence\":[]}。"
    )
    try:
        payload, _meta, _raw = _call_json_payload_once_on_format_error(
            model, repair_prompt, call,
            phase=f"逐页账本证据重新定位（第 {page_no} 页）")
    except SceneModelError:
        return True
    new_evidence = payload.get("evidence")
    if not isinstance(new_evidence, list):
        return True
    valid_evidence = [str(x).strip() for x in new_evidence if str(x).strip()
                      and _excerpt_matches(page_text, str(x).strip())]
    row["evidence"] = valid_evidence
    return True


def _accepts_keyword(func, name: str) -> bool:
    """Whether an injected provider's generate_one can take an optional hint."""
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    return name in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values())


def build_plan(source_pptx: str | Path, layout_pptx: str | Path | None = None,
               *, cache_dir: str | Path | None = None, visual_plan: dict | None = None,
               model: str | None = None, model_call=None, provider=None,
               semantic_resolution: dict | None = None,
               visual_review_call=None,
               allow_text_card_fallback: bool = False) -> dict:
    """Build one licensed photo placement per concrete source-grounded scene.

    Local semantic inference and remote image retrieval are deliberately separate:
    only a neutral English visual query is sent to photo providers. The returned
    plan freezes page evidence, provider/license metadata, image bytes, and exact
    placement for later no-network validation.

    ``allow_text_card_fallback`` (default off, explicit user opt-in only): a
    scene that could not get a verified, licensed photo (not a capacity issue —
    those already deliver non-blockingly) is, instead of blocking the whole
    deck, given a plainly-labeled text card in a genuinely empty safe area if
    one exists. It never replaces an existing generic image (that would trade
    a real picture for a vaguer placeholder) and never claims to be a photo.
    """
    source_pptx = Path(source_pptx)
    layout_pptx = Path(layout_pptx or source_pptx)
    if not source_pptx.is_file() or not layout_pptx.is_file():
        raise SemanticSceneError("SCENE.INPUT_MISSING", "场景规划缺少来源或版式 PPTX。")
    source_pages = _source_pages(source_pptx)
    layout = Presentation(str(layout_pptx))
    if len(layout.slides) != len(source_pages):
        raise SemanticSceneError("SCENE.PAGE_COUNT_MISMATCH", "场景规划来源与版式页数不一致。")
    model_generated = semantic_resolution is None
    model_health = None
    if model_generated:
        # 开跑前先确认端点真的在应答：否则要等几分钟文本规划后，才在最深处的
        # 图片复核处以一个读不懂的连接错误失败。只对真实运行预检——注入了
        # model_call 的调用（测试/夹具）根本不碰端点。是否需要视觉端点沿用现有
        # 规则：真实模型规划 + 未注入复核器 + 真实 ScenePhotoProvider 才复核图片。
        if model_call is None:
            needs_vision_review = bool(
                visual_review_call is None
                and (provider is None or isinstance(provider, ScenePhotoProvider)))
            model_health = preflight_scene_models(needs_vision=needs_vision_review)
        semantic_resolution, model_meta = resolve_scene_semantics(
            source_pages, model=model, model_call=model_call)
    else:
        model_meta = {"provider": "injected-test-or-caller", "response_sha256":
                      _canonical_sha(semantic_resolution)}
    dedupe_audit: list[dict] = []
    repairable_codes = ("SCENE.EVIDENCE_NOT_GROUNDED", "SCENE.PAGE_EVIDENCE_NOT_GROUNDED")
    # Scene-candidate evidence and page_coverage-ledger evidence are two
    # independent fields a real model response can each separately
    # mis-ground. A single response can hit both (or the same kind twice for
    # two different scenes/pages). The retried _normalize_semantics call used
    # to run with no exception handling at all, so fixing the first failure
    # only to hit a second, equally repairable one on the retry hard-failed
    # the whole deck with zero protection on that second failure. Loop with a
    # bound of one retry per repairable failure kind instead of a single
    # fixed retry.
    max_normalize_attempts = 1 + len(repairable_codes)
    for attempt in range(max_normalize_attempts):
        try:
            scenes, page_coverage = _normalize_semantics(
                semantic_resolution, source_pages,
                require_canonical_scene_id=model_generated,
                dedupe_audit=dedupe_audit)
            break
        except SemanticSceneError as exc:
            if (not model_generated or exc.code not in repairable_codes
                    or attempt == max_normalize_attempts - 1):
                raise
            call = model_call or call_local_model
            repair_model = model or os.environ.get("WENGAO_SCENE_MODEL", "qwen3.8:27b-wb65536")
            if exc.code == "SCENE.EVIDENCE_NOT_GROUNDED":
                repaired = _repair_scene_evidence_grounding(
                    semantic_resolution, source_pages, exc, model=repair_model, call=call)
            else:
                repaired = _repair_page_coverage_evidence(
                    semantic_resolution, source_pages, exc, model=repair_model, call=call)
            if not repaired:
                raise
            dedupe_audit = []
    if not scenes:
        unresolved = [{"scene_key": None, "reason": "SCENE.NO_SCENES_PLANNED",
                       "detail": "整套稿件没有一个有原文依据的具体场景；场景图硬规则不允许纯文字通过。"}]
    else:
        unresolved = []
    slots = _visual_slots(visual_plan, layout=layout)
    page_counts = Counter()
    occupied: dict[int, list[list[float]]] = {}

    if provider is None:
        if cache_dir is None:
            cache_dir = source_pptx.parent / ".wengao_scene_assets"
        review_model = str(model_meta.get("model") or model or
                           "qwen3.8:27b-wb65536")
        visual_reviewer = (
            (lambda scene, image_bytes: visual_review_call(
                review_model, scene, image_bytes)) if visual_review_call else
            (lambda scene, image_bytes: review_semantic_scene_image(
                review_model, scene, image_bytes)) if model_generated else None)
        provider = ScenePhotoProvider(cache_dir,
                                      visual_reviewer=visual_reviewer)
    elif model_generated and isinstance(provider, ScenePhotoProvider):
        review_model = str(model_meta.get("model") or model or
                           "qwen3.8:27b-wb65536")
        if visual_review_call:
            provider.visual_reviewer = lambda scene, image_bytes: visual_review_call(
                review_model, scene, image_bytes)
        elif provider.visual_reviewer is None:
            provider.visual_reviewer = lambda scene, image_bytes: review_semantic_scene_image(
                review_model, scene, image_bytes)
    images = {}
    used_hashes, used_sources, used_phashes = set(), set(), []
    forced_page_scene_counts = Counter(
        scene["page_candidates"][0]["page"] for scene in scenes
        if len(scene.get("page_candidates") or []) == 1)
    paired_photo_pages = {page for page, count in forced_page_scene_counts.items()
                          if count == 2}
    overcrowded_forced_pages = {page for page, count in forced_page_scene_counts.items()
                                if count > _MAX_SCENES_PER_PAGE}
    for scene in sorted(scenes, key=lambda s: (len(s["page_candidates"]), s["scene_key"])):
        try:
            if (len(scene.get("page_candidates") or []) == 1
                    and scene["page_candidates"][0]["page"] in overcrowded_forced_pages):
                unresolved.append({"scene_key": scene["scene_key"],
                                   "reason": "SCENE.PAGE_SCENE_CAPACITY_EXCEEDED",
                                   "detail": "同一页需要多张独立场景图，已超过可安全排版的图片数量；需先重排内容。"})
                continue
            _privacy_query_check(scene, source_pages)
            photo_match_context = {**scene, "metadata_query": scene["visual_query"],
                                   "visual_queries": scene.get("visual_queries") or
                                                       [scene["visual_query"]]}

            def _generated_review_valid(img: dict) -> bool:
                visual_review = img.get("visual_review")
                image_path = Path(img.get("file") or "")
                try:
                    image_bytes = image_path.read_bytes() if image_path.is_file() else b""
                    expected_prompt_sha = hashlib.sha256(
                        scene_image_review_prompt(photo_match_context).encode("utf-8")
                    ).hexdigest()
                    expected_result_sha = scene_review_result_sha256(
                        visual_review if isinstance(visual_review, dict) else {})
                    expected_input_sha = scene_review_payload_sha256(image_bytes)
                except Exception:
                    expected_prompt_sha = expected_result_sha = expected_input_sha = ""
                response_sha = (str(visual_review.get("response_sha256") or "")
                                if isinstance(visual_review, dict) else "")
                return bool(
                    isinstance(visual_review, dict)
                    and visual_review.get("schema_version") == "WG-LOCAL-SCENE-IMAGE-REVIEW/1"
                    and visual_review.get("status") == "PASSED"
                    and visual_review.get("image_sha256") == str(img.get("sha256") or "")
                    and visual_review.get("visual_input_sha256") == expected_input_sha
                    and visual_review.get("scene_identity_sha256") ==
                        scene_visual_identity_sha256(scene)
                    and visual_review.get("prompt_sha256") == expected_prompt_sha
                    and visual_review.get("review_result_sha256") == expected_result_sha
                    and re.fullmatch(r"[0-9a-f]{64}", response_sha) is not None
                    and bool(str(visual_review.get("model") or "").strip())
                    and all(visual_review.get(field) is True for field in (
                        "scene_match", "subject_visible", "action_visible",
                        "setting_visible", "photographic"))
                    and visual_review.get("confidence") == "high"
                    and visual_review.get("reason_code") == "NONE")

            image, selected_query, query_failures = None, None, []
            paired_aspect = (1.52 if len(scene.get("page_candidates") or []) == 1
                             and scene["page_candidates"][0]["page"] in paired_photo_pages
                             else None)
            queries = scene.get("visual_queries") or [scene["visual_query"]]
            # The real provider performs one bounded search over the canonical
            # query plus its planner-approved synonyms. Retrying fetch_one once
            # per synonym used to repeat the same anchor-derived provider queries
            # (the provider ignored planner alternatives), multiplying timeouts
            # without improving recall. Keep the legacy loop for injected/simple
            # providers that do not implement the scene-search contract.
            provider_queries = (queries[:1] if isinstance(provider, ScenePhotoProvider)
                                else queries)
            for query in provider_queries:
                try:
                    image = provider.fetch_one(query,
                                               excluded_sha256=used_hashes,
                                               excluded_source_keys=used_sources,
                                               excluded_perceptual_hashes=used_phashes,
                                               scene_context=photo_match_context,
                                               min_aspect=paired_aspect)
                    selected_query = query
                    break
                except SceneImageError as exc:
                    query_failures.append({"query": query, "code": exc.code,
                                           "detail": exc.detail,
                                           **({"diagnostics": exc.diagnostics}
                                              if getattr(exc, "diagnostics", None) else {})})
            if image is None:
                # 免费图库穷尽后，退回 AI 生图（本机 ComfyUI，或用户显式配置的云端
                # 生图服务——由 generate_one 内部按 cloud_model_config 决定）：
                # generate_one 内部已经调用视觉复核，未通过时直接抛出
                # SceneImageError（不是"随便画一张凑数"）。生图按随机种子/采样，
                # 第一次复核没通过就立刻放弃等于把"能不能配图"交给一次随机抽样。
                # 复核给出的 BLOCKED 结论带有具体原因（主体/动作/环境哪一项看不见），
                # 所以重试不是"再抽一次"，而是把这一项角色在提示词里显式强调后再生成；
                # 仍然由同一套像素复核判定。上限 3 次，硬失败（服务不可用等）立即停。
                generate_one = getattr(provider, "generate_one", None)
                if callable(generate_one):
                    gen_attempts = 3
                    retryable_codes = {"SCENE.VISUAL_CONTENT_MISMATCH",
                                       "SCENE.GEN_IMAGE_NOT_PHOTOGRAPHIC",
                                       "SCENE.GEN_IMAGE_NEAR_DUPLICATE",
                                       "SCENE.GEN_IMAGE_DUPLICATE",
                                       # 服务端没在预算内回图属于上游抖动，不是语义
                                       # 判否：一次空响应就把场景钉死会让整稿因为一次
                                       # 超时失败（2026-10-26 job-c0af57f36099 第 8/9 页）。
                                       "SCENE.GEN_IMAGE_FAILED"}
                    focus_role = None
                    for gen_attempt in range(gen_attempts):
                        try:
                            gen_kwargs = dict(
                                scene_context=photo_match_context,
                                excluded_sha256=used_hashes,
                                excluded_source_keys=used_sources,
                                excluded_perceptual_hashes=used_phashes,
                                min_aspect=paired_aspect)
                            if focus_role and _accepts_keyword(generate_one,
                                                               "review_focus_role"):
                                gen_kwargs["review_focus_role"] = focus_role
                            image = generate_one(**gen_kwargs)
                            selected_query = scene["visual_query"]
                            break
                        except SceneImageError as exc:
                            query_failures.append({"query": "ai_image_generation",
                                                   "code": exc.code, "detail": exc.detail,
                                                   **({"diagnostics": exc.diagnostics}
                                                      if getattr(exc, "diagnostics", None) else {})})
                            focus_role = (review_missing_role(
                                getattr(exc, "diagnostics", None)) or focus_role)
                            if (exc.code in _GEN_IMAGE_BACKOFF_CODES
                                    and gen_attempt < gen_attempts - 1):
                                # 上游空响应/超时：短暂退避后再要一次，别把一次抖动
                                # 当成"没有可用场景图"。
                                time.sleep(min(8.0, 2.0 * (gen_attempt + 1)))
                            if (exc.code not in retryable_codes
                                    or gen_attempt == gen_attempts - 1):
                                break
            if image is None:
                last = query_failures[-1] if query_failures else {}
                unresolved.append({"scene_key": scene["scene_key"],
                                   "reason": last.get("code", "SCENE.NO_LICENSED_PHOTO"),
                                   "detail": last.get("detail", "备选查询均未找到许可合规、相关且清晰的场景照片；本机生图亦不可用。"),
                                   "query_attempts": query_failures})
                continue
            if model_generated and not image.get("visual_review"):
                review_model = str(model_meta.get("model") or model or
                                   "qwen3.8:27b-wb65536")
                review_call = visual_review_call or review_semantic_scene_image
                try:
                    image_path = Path(image.get("file") or "")
                    if not image_path.is_file():
                        raise SceneModelError("SCENE.VISION_IMAGE_INVALID",
                                              "下载图片缺少可复核字节。")
                    visual_review = review_call(review_model, photo_match_context,
                                                image_path.read_bytes())
                except SceneModelError as exc:
                    if exc.code.startswith("MODEL."):
                        raise
                    unresolved.append({"scene_key": scene["scene_key"],
                                       "reason": "SCENE.VISUAL_REVIEW_UNAVAILABLE",
                                       "detail": exc.detail})
                    continue
                except Exception:
                    unresolved.append({"scene_key": scene["scene_key"],
                                       "reason": "SCENE.VISUAL_REVIEW_UNAVAILABLE",
                                       "detail": "本机图片语义复核没有完成；该场景图被阻断。"})
                    continue
                image["visual_review"] = visual_review
            if model_generated:
                review_valid = _generated_review_valid(image)
                if not review_valid:
                    # 图片字节会被丢弃，判定必须留下来，否则事后无从分辨
                    # 到底是模型判否还是绑定校验没过。
                    unresolved.append({"scene_key": scene["scene_key"],
                                       "reason": "SCENE.VISUAL_CONTENT_MISMATCH",
                                       "detail": "本机视觉模型未能以高置信度确认照片中的主体、动作、环境和绑定身份。",
                                       "visual_review": scene_review_verdict_digest(
                                           image.get("visual_review"))})
                    continue
        except SemanticSceneError as exc:
            unresolved.append({"scene_key": scene["scene_key"],
                               "reason": exc.code, "detail": exc.detail})
            continue
        # 图库元数据门禁校验的是**第三方**资产的标题/说明能否证明它就是这个场景。
        # AI 生图的"元数据"是我们自己的提示词，用同一套锚点去校验它是循环论证；
        # 它的证据是出图后立即执行的像素复核（generate_one 内部已完成，且
        # validate_output 会按嵌入字节重新绑定核验）。所以这两类资产的元数据门禁
        # 不适用，其余许可/尺寸/去重/版位门禁一律照旧。
        ai_generated_asset = str(image.get("license") or "") in ("AI_GENERATED_LOCAL",
                                                                 "AI_GENERATED_CLOUD")
        if ai_generated_asset:
            relevance = float(image.get("metadata_relevance") or 1.0)
            match_query = scene["visual_query"]
        else:
            relevance = scene_metadata_relevance(scene["visual_query"], image,
                                                 scene_context=photo_match_context)
            if not photo_style_matches_metadata(scene.get("style"), image):
                unresolved.append({"scene_key": scene["scene_key"],
                                   "reason": "SCENE.PHOTO_STYLE_MISMATCH",
                                   "detail": "照片元数据标记为插画/绘画等非摄影素材，与场景要求不符。"})
                continue
            if not scene_metadata_matches_query(scene["visual_query"], image,
                                                scene_context=photo_match_context):
                unresolved.append({"scene_key": scene["scene_key"],
                                   "reason": "SCENE.IMAGE_CONTEXT_UNVERIFIED",
                                   "detail": "图片标题/说明没有同时支持场景主体、动作和地点锚点。"})
                continue
            match_query = scene["visual_query"]
            query_terms = {term for term in re.findall(r"[a-z0-9]+", match_query.lower())
                           if len(term) >= 3 and term not in {
                               "the", "and", "with", "from", "into", "while", "photo",
                               "image", "picture", "photograph", "wide", "landscape",
                               "scene", "at", "in", "on", "of", "a", "an", "to"}}
            required_terms = min(2, len(query_terms))
            matched_terms = int(round(relevance * len(query_terms)))
            if (relevance < MIN_METADATA_RELEVANCE or matched_terms < required_terms
                    or not image.get("title")):
                unresolved.append({"scene_key": scene["scene_key"],
                                   "reason": "SCENE.IMAGE_RELEVANCE_UNVERIFIED",
                                   "detail": "下载后重新核对标题/说明，来源元数据不足以证明照片对应该场景。"})
                continue
        license_name = image.get("license", "")
        if license_name in ("AI_GENERATED_LOCAL", "AI_GENERATED_CLOUD"):
            # Only generate_one() ever sets these literal strings, deterministically,
            # for a render from the local ComfyUI service or a user-configured
            # cloud image-generation service — never a value a stock provider or
            # a model response can produce. Neither has an external rightsholder
            # or source page to verify; its provenance guarantee is that it was
            # produced by the configured generation service, not a checkable URL.
            license_ok = bool(image.get("source_key")) and bool(
                str(image.get("creator") or image.get("attribution") or "").strip())
        else:
            license_ok = (
                str(image.get("source_page") or "").startswith("https://")
                and (str(image.get("provider") or "").lower() != "pexels"
                     or is_valid_pexels_source_page(image.get("source_page") or ""))
                and (str(image.get("provider") or "").lower() != "pixabay"
                     or is_valid_pixabay_source_page(image.get("source_page") or ""))
                and bool(image.get("source_key"))
                and bool(str(image.get("creator") or image.get("attribution") or "").strip())
                and is_allowed_license(
                    license_name, version=image.get("license_version"),
                    license_url=image.get("license_url"))
                and (not image.get("license_url") or is_valid_license_url(
                    image.get("license_url"), license_name)))
        if not license_ok:
            unresolved.append({"scene_key": scene["scene_key"],
                               "reason": "SCENE.LICENSE_METADATA_MISSING",
                               "detail": "照片缺少来源、作者/署名或可核验的许可元数据。"})
            continue
        if not is_safe_metadata(image):
            unresolved.append({"scene_key": scene["scene_key"],
                               "reason": "SCENE.CONTENT_SAFETY_METADATA_BLOCK",
                               "detail": "照片来源元数据标记了不适合普通演示文稿的内容。"})
            continue
        sha = str(image.get("sha256") or "")
        source_key = str(image.get("source_key") or "")
        image_file = Path(image.get("file") or "")
        actual_file_sha = _sha(image_file) if image_file.is_file() else ""
        if not sha or sha != actual_file_sha or not source_key or sha in used_hashes or source_key in used_sources:
            unresolved.append({"scene_key": scene["scene_key"],
                               "reason": "SCENE.PHOTO_REUSED_OR_UNVERIFIED",
                               "detail": "场景照片的真实字节或来源身份缺失/重复。"})
            continue
        phash = str(image.get("perceptual_hash") or
                    perceptual_hash(Path(image["file"]).read_bytes()))
        if any(image_hash_distance(phash, old) <= 4 for old in used_phashes):
            unresolved.append({"scene_key": scene["scene_key"],
                               "reason": "SCENE.NEAR_DUPLICATE_PHOTO",
                               "detail": "该照片与本稿已有照片过于相似；需要不同的场景素材。"})
            continue
        try:
            image_width, image_height = int(image.get("actual_width") or 0), int(image.get("actual_height") or 0)
            if image_width <= 0 or image_height <= 0:
                with Image.open(image_file) as source_image:
                    image_width, image_height = source_image.size
            image_aspect = image_width / image_height
        except Exception:
            unresolved.append({"scene_key": scene["scene_key"],
                               "reason": "SCENE.IMAGE_DIMENSIONS_INVALID",
                               "detail": "无法核验场景照片尺寸，不能安全选择不裁切版位。"})
            continue
        assignment = _choose_assignment(scene, source_pages, layout, page_counts,
                                        occupied, slots, image_aspect=image_aspect,
                                        paired_photo_pages=paired_photo_pages)
        if assignment is None:
            unresolved.append({"scene_key": scene["scene_key"],
                               "reason": "SCENE.NO_SAFE_PHOTO_AREA",
                               "detail": "没有足够大的无文字遮挡且能完整容纳照片的区域；需先重排页面。"})
            continue
        page_counts[assignment["page"]] += 1
        occupied.setdefault(assignment["page"], []).append(assignment["rect"])
        used_hashes.add(sha)
        used_sources.add(source_key)
        used_phashes.append(phash)
        candidate = next(c for c in scene["page_candidates"]
                         if c["page"] == assignment["page"])
        image_spec = {
            "scene_key": scene["scene_key"], "topic": scene["topic"],
            "subject": scene["subject"], "action": scene["action"],
            "setting": scene["setting"], "label": scene["label"],
            "depiction_type": scene.get("depiction_type", "illustrative"),
            "illustration_rationale": scene.get("illustration_rationale", ""),
            "image_prompt_fields": scene.get("image_prompt_fields"),
            "image_plan": scene.get("image_plan"),
            "visual_query": selected_query,
            "query": selected_query,
            "visual_query_candidates": scene.get("visual_queries") or [scene["visual_query"]],
            "page": assignment["page"], "evidence": candidate["evidence"],
            "file": image["file"], "sha256": sha,
            "content_hash": sha,
            "download_url": str(image.get("download_url") or ""),
            "asset_id": str(image.get("asset_id") or image.get("id") or source_key),
            "perceptual_hash": phash, "bytes": image.get("bytes"),
            "source_key": source_key, "provider": image.get("provider"),
            "title": image.get("title"), "description": image.get("description"),
            "creator": image.get("creator"),
            "source_page": image.get("source_page"), "license": image.get("license"),
            "license_version": image.get("license_version"),
            "license_url": image.get("license_url"), "attribution": image.get("attribution"),
            "actual_format": image.get("actual_format"),
            "visual_review": image.get("visual_review"),
            "actual_width": image_width, "actual_height": image_height,
            # Never trust a provider-supplied score; freeze the value recomputed
            # from the selected neutral query and title/description metadata.
            "metadata_relevance": round(relevance, 6),
            "rect": assignment["rect"], "placement_mode": assignment["mode"],
            "fit_mode": assignment["fit_mode"],
            "replaced_visual_sha256": assignment.get("replaced_visual_sha256"),
            "area_ratio": round(assignment["quality"]["area_ratio"], 6),
            "text_overlap_ratio": round(assignment["quality"]["text_overlap_ratio"], 6),
            "focus": [0.5, 0.5], "model_relevance": assignment["relevance"],
            "candidate_pages": scene["candidate_pages"],
            "reflow_shapes": assignment.get("reflow_shapes", []),
            "layout_mode": "paired_photo_row" if assignment["mode"] == "paired_photo_row" else "single_photo_area",
        }
        images[scene["scene_key"]] = image_spec

    text_cards: dict[str, dict] = {}
    if allow_text_card_fallback and unresolved:
        scenes_by_key = {s["scene_key"]: s for s in scenes}
        still_unresolved = []
        for item in unresolved:
            reason = (item or {}).get("reason")
            scene = scenes_by_key.get((item or {}).get("scene_key"))
            if reason in CAPACITY_UNRESOLVED_REASONS or scene is None:
                still_unresolved.append(item)
                continue
            assignment = _choose_assignment(scene, source_pages, layout, page_counts,
                                            occupied, slots, image_aspect=None,
                                            paired_photo_pages=paired_photo_pages)
            # Never displace an existing generic image for a vaguer text card —
            # that would be a net loss, not a fallback. Only a genuinely empty
            # safe area qualifies.
            if assignment is None or assignment["mode"] == "replace_visual_image":
                still_unresolved.append(item)
                continue
            page_counts[assignment["page"]] += 1
            occupied.setdefault(assignment["page"], []).append(assignment["rect"])
            text_cards[scene["scene_key"]] = {
                "scene_key": scene["scene_key"], "page": assignment["page"],
                "rect": assignment["rect"], "placement_mode": assignment["mode"],
                "fit_mode": assignment["fit_mode"],
                "reflow_shapes": assignment.get("reflow_shapes", []),
                "label": scene.get("label", ""), "subject": scene.get("subject", ""),
                "action": scene.get("action", ""), "setting": scene.get("setting", ""),
                "fallback_reason": reason, "fallback_detail": item.get("detail", ""),
            }
        # 找遍图库、本机看图也没有认可任何一张、又没有空白版位放说明卡片：
        # 这个场景就不配图，如实登记为"未放置"，不再拦下整份稿子（用户
        # 2026-10-29 决定）。只限"没找到可核验照片"这一类；隐私、许可、
        # 去重等真实性问题仍然阻断。
        unresolved = [
            ({"scene_key": item.get("scene_key"), "reason": NO_VERIFIED_PHOTO_SKIPPED,
              "original_reason": item.get("reason"), "detail": (
                  "没有找到经本机看图复核认可的照片，也没有空白版位放说明卡片；"
                  "该场景不配图，其余内容照常交付。"),
              "query_attempts": item.get("query_attempts", [])}
             if (item or {}).get("reason") in NO_PHOTO_FOUND_REASONS else item)
            for item in still_unresolved
        ]

    for row in page_coverage:
        row["image_scene_keys"] = [k for k, spec in images.items() if spec["page"] == row["page"]]
        row["text_card_scene_keys"] = [k for k, spec in text_cards.items()
                                       if spec["page"] == row["page"]]
    # 放不下（版位容量）与放不得（真实性/合规）分开登记：
    # 前者如实记为"未放置"并继续交付，后者仍然整单阻断（除非上面已经
    # 用说明卡片顶上，那就已经从 unresolved 里移出去了）。
    blocking_unresolved, capacity_unplaced = split_unresolved_scenes(unresolved)
    plan = {
        "schema_version": "2.3.0", "kind": "semantic_scene_plan",
        "rules_version": VERSION, "gate_id": SCENE_GATE_ID,
        "policy_sha256": _sha(POLICY_FILE),
        "source_pptx_sha256": _sha(source_pptx),
        "layout_input_sha256": _sha(layout_pptx),
        "source_page_count": len(source_pages),
        "language_support": language_support_record(source_pages),
        "planner": {"kind": "open_vocabulary_local_model",
                    "requested_model": model_meta.get("requested_model"),
                    "model": model_meta.get("model"),
                    "response_sha256": model_meta.get("response_sha256"),
                    "elapsed_seconds": model_meta.get("elapsed_seconds"),
                    "planner_mode": model_meta.get("planner_mode"),
                    "planner_batch_page_limit": model_meta.get("planner_batch_page_limit"),
                    "planner_batches": model_meta.get("planner_batches", []),
                    "truncation_splits": model_meta.get("truncation_splits", []),
                    "photo_search_anchor_repairs": model_meta.get(
                        "photo_search_anchor_repairs", [])},
        "coverage_audit": (model_meta.get("coverage_audit") or {
            "schema_version": "1.0.0",
            "status": "UNVERIFIED_INJECTED_TEST_OR_CALLER",
            "pages_checked": 0,
        }),
        "photo_provider_policy": "CC0/PDM/Public Domain, CC BY 4.0 with attribution, or opt-in Pexels License with creator/source links; only short neutral English photo queries sent remotely; metadata gate remains bound to original structured scene anchors",
        "dedupe_rule": "one placement per canonical specific scene and each embedded photo SHA/source key at most once deck-wide",
        "dedupe_audit": dedupe_audit,
        "visual_review_policy": ("LOCAL_MULTIMODAL_REQUIRED" if model_generated
                                 else "NOT_PRODUCTION_TEST_FIXTURE"),
        "page_coverage": page_coverage,
        "scenes": [{k: v for k, v in scene.items() if not k.startswith("_")}
                   for scene in scenes],
        "detected_scene_ids": [scene["scene_key"] for scene in scenes],
        "images": images, "unresolved_scenes": blocking_unresolved,
        "unplaced_scenes": capacity_unplaced,
        "text_cards": text_cards,
        "photo_provider_errors": list(getattr(provider, "errors", []) or []),
    }
    if model_health is not None:
        plan["model_health"] = model_health
    plan["plan_sha256"] = _plan_hash(plan)
    return plan


def _marked_shapes(pptx_path: str | Path) -> list[dict]:
    prs = Presentation(str(pptx_path))
    out = []
    for page, slide in enumerate(prs.slides, 1):
        for shape in slide.shapes:
            if getattr(shape, "shape_type", None) != MSO_SHAPE_TYPE.PICTURE:
                continue
            c_nv = shape._element.nvPicPr.cNvPr
            marker = str(c_nv.get("name") or "")
            if not marker.startswith("WENGAO_SCENE|"):
                continue
            parts = marker.split("|", 2)
            actual_sha = _picture_sha(shape)
            rect = _shape_rect(shape)
            try:
                actual_phash = perceptual_hash(shape.image.blob)
            except Exception:
                actual_phash = None
            shape_id = getattr(shape, "shape_id", None)
            shape_name = getattr(shape, "name", None)
            out.append({"page": page, "scene_key": parts[1] if len(parts) >= 2 else None,
                        "marker_sha256": parts[2] if len(parts) == 3 else None,
                        "actual_sha256": actual_sha,
                        "actual_perceptual_hash": actual_phash,
                        "shape_id": shape_id,
                        "shape_name": shape_name,
                        "object_id": shape_id,
                        "object_name": shape_name,
                        "rect": [round(float(x), 4) for x in rect],
                        "aspect_ratio": float(shape.width) / max(1, float(shape.height)),
                        "crop": [float(shape.crop_left), float(shape.crop_top),
                                 float(shape.crop_right), float(shape.crop_bottom)],
                        "area_ratio": _rect_area(rect) /
                                      max(1, prs.slide_width * prs.slide_height / (_IN * _IN)),
                        "alt_text": str(c_nv.get("descr") or ""),
                        "malformed": len(parts) != 3 or not actual_sha})
    return out


def _scene_picture_pixel_inputs(pptx_path: str | Path) -> tuple[Presentation, dict[str, list[dict]]]:
    """Read actual embedded scene-picture bytes and exact OOXML geometry for pixel checks."""
    prs = Presentation(str(pptx_path))
    by_key: dict[str, list[dict]] = {}
    for page, slide in enumerate(prs.slides, 1):
        for shape in slide.shapes:
            if getattr(shape, "shape_type", None) != MSO_SHAPE_TYPE.PICTURE:
                continue
            marker = str(shape._element.nvPicPr.cNvPr.get("name") or "")
            if not marker.startswith("WENGAO_SCENE|"):
                continue
            parts = marker.split("|", 2)
            key = parts[1] if len(parts) >= 2 else ""
            by_key.setdefault(key, []).append({
                "page": page,
                "marker_sha256": parts[2] if len(parts) == 3 else None,
                "actual_sha256": _picture_sha(shape),
                "image_blob": bytes(shape.image.blob),
                "emu_rect": [int(shape.left), int(shape.top),
                             int(shape.width), int(shape.height)],
                "crop": [float(shape.crop_left), float(shape.crop_top),
                         float(shape.crop_right), float(shape.crop_bottom)],
            })
    return prs, by_key


def make_scene_suppressed_pptx(candidate_pptx: str | Path,
                               output_pptx: str | Path) -> dict:
    """Create a render-only reference PPTX with only marked scene pictures removed.

    All non-slide package members are copied byte-for-byte. Slide XML changes only
    by removal of p:pic nodes whose cNvPr name begins with ``WENGAO_SCENE|``. The
    reference is used to measure whether each scene photo contributes visible pixels.
    """
    source, output = Path(candidate_pptx), Path(output_pptx)
    if not source.is_file() or source.resolve() == output.resolve():
        raise ValueError("a distinct existing candidate and reference output are required")
    output.parent.mkdir(parents=True, exist_ok=True)
    slide_re = re.compile(r"^ppt/slides/slide\d+\.xml$")
    removed = []
    with zipfile.ZipFile(source, "r") as zin, zipfile.ZipFile(output, "w") as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if slide_re.fullmatch(info.filename):
                root = etree.fromstring(data)
                slide_no = int(re.search(r"slide(\d+)\.xml$", info.filename).group(1))
                for picture in root.xpath(
                        ".//p:pic", namespaces={
                            "p": "http://schemas.openxmlformats.org/presentationml/2006/main"}):
                    marker_node = picture.find(
                        ".//{http://schemas.openxmlformats.org/presentationml/2006/main}cNvPr")
                    marker = str(marker_node.get("name") or "") if marker_node is not None else ""
                    if marker.startswith("WENGAO_SCENE|"):
                        parts = marker.split("|", 2)
                        removed.append({"slide": slide_no,
                                        "scene_key": parts[1] if len(parts) > 1 else None,
                                        "marker_sha256": parts[2] if len(parts) > 2 else None})
                        picture.getparent().remove(picture)
                if any(row["slide"] == slide_no for row in removed):
                    data = etree.tostring(root, xml_declaration=True, encoding="UTF-8",
                                          standalone=True)
            zout.writestr(info, data)
    if not removed:
        output.unlink(missing_ok=True)
        raise ValueError("candidate contains no marked semantic scene pictures")
    return {"source_pptx_sha256": _sha(source),
            "scene_suppressed_pptx_sha256": _sha(output),
            "removed_scene_pictures": removed,
            "removed_count": len(removed)}


def _tile_match_ratio(rendered: Image.Image, expected: Image.Image) -> tuple[float, int, int, float]:
    """Compare a rendered photo rectangle with its embedded source in small tiles.

    Tile means tolerate normal PDF rasterization/color-management differences while
    ensuring that a large opaque overlay cannot pass because a few pixels remain.
    This measures photo visibility only; scene relevance still requires source review.
    """
    actual = rendered.convert("RGB")
    reference = expected.convert("RGB")
    if actual.size != reference.size or min(actual.size) < _SCENE_RENDER_GRID:
        return 0.0, 0, 0, 255.0
    matched = total = 0
    errors = []
    width, height = actual.size
    for row in range(_SCENE_RENDER_GRID):
        y0, y1 = row * height // _SCENE_RENDER_GRID, (row + 1) * height // _SCENE_RENDER_GRID
        if y1 <= y0:
            continue
        for col in range(_SCENE_RENDER_GRID):
            x0 = col * width // _SCENE_RENDER_GRID
            x1 = (col + 1) * width // _SCENE_RENDER_GRID
            if x1 <= x0:
                continue
            box = (x0, y0, x1, y1)
            difference = ImageChops.difference(actual.crop(box), reference.crop(box))
            mae = sum(ImageStat.Stat(difference).mean[:3]) / 3.0
            errors.append(mae)
            total += 1
            if mae <= _SCENE_RENDER_MAX_TILE_MAE:
                matched += 1
    return (matched / total if total else 0.0, matched, total,
            sum(errors) / len(errors) if errors else 255.0)


def validate_rendered_scene_visibility(candidate_pptx: str | Path,
                                       plan_path: str | Path | None,
                                       render_evidence: dict | None) -> dict:
    """Fail closed unless exact-candidate full-page renders show each planned photo.

    ``render_evidence`` contract (WG-SEMANTIC-SCENE-RENDER-EVIDENCE/1):
      {
        "schema": "WG-SEMANTIC-SCENE-RENDER-EVIDENCE/1",
        "candidate_pptx_sha256": <sha256>, "scene_plan_sha256": <sha256>,
        "scene_suppressed_pptx_sha256": <sha256>,
        "policy_sha256": <POLICY_SHA256>, "rendered_pdf_sha256": <sha256>,
        "rendered_pdf_path": <absolute path>,
        "suppressed_rendered_pdf_sha256": <sha256>,
        "suppressed_rendered_pdf_path": <absolute path>,
        "renderer": {"name": <renderer>, "version": <version>},
        "pages": [{"page": 1, "png_path": <absolute path>, "png_sha256": <sha256>,
                   "suppressed_png_path": <absolute path>,
                   "suppressed_png_sha256": <sha256>}, ...]
      }

    The caller must obtain two real full-page renders with the same renderer: the
    exact candidate and a reference created by ``make_scene_suppressed_pptx``. This
    function verifies the reference PPTX hash, re-hashes each page image, checks its
    dimensions/page mapping, measures pixel contribution, and compares photo pixels
    with the exact bytes embedded in the candidate PPTX.
    """
    result = {"schema_version": "1.0.0", "gate_id": "HG13_SCENE_PIXEL_VISIBILITY",
              "status": "NOT_EVALUABLE", "blocking": True,
              "findings": [], "unverified": [], "summary": {}}
    findings, unverified = result["findings"], result["unverified"]

    def fail(code: str, detail: str, **extra) -> None:
        findings.append({"code": code, "severity": "BLOCKING", "detail": detail, **extra})

    def unknown(code: str, detail: str, **extra) -> None:
        unverified.append({"code": code, "detail": detail, **extra})

    try:
        candidate_path = Path(candidate_pptx)
        if not candidate_path.is_file():
            unknown("SCENE.RENDER_CANDIDATE_MISSING", "像素门禁找不到候选 PPTX。")
            raise ValueError("candidate PPTX missing")
        candidate_sha = _sha(candidate_path)
        result["summary"]["candidate_pptx_sha256"] = candidate_sha
        if not plan_path or not Path(plan_path).is_file():
            unknown("SCENE.RENDER_PLAN_MISSING", "像素门禁缺少冻结场景计划。")
            raise ValueError("scene plan missing")
        plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
        plan_sha = str(plan.get("plan_sha256") or "")
        result["summary"]["scene_plan_sha256"] = plan_sha or None
        if not plan_sha or plan_sha != _plan_hash(plan):
            fail("SCENE.RENDER_PLAN_IDENTITY_INVALID", "场景计划哈希无效。")
        if _sha(POLICY_FILE) != POLICY_SHA256 or plan.get("policy_sha256") != POLICY_SHA256:
            fail("SCENE.RENDER_POLICY_IDENTITY_MISMATCH", "像素门禁策略身份不匹配。")
        if not isinstance(render_evidence, dict):
            unknown("SCENE.RENDER_EVIDENCE_MISSING", "缺少候选逐页真实渲染证据。")
            raise ValueError("render evidence missing")
        if render_evidence.get("schema") != _SCENE_RENDER_EVIDENCE_SCHEMA:
            fail("SCENE.RENDER_EVIDENCE_SCHEMA_INVALID", "渲染证据格式缺失或不受支持。")
        if render_evidence.get("candidate_pptx_sha256") != candidate_sha:
            fail("SCENE.RENDER_CANDIDATE_IDENTITY_MISMATCH", "渲染证据未绑定当前候选 PPTX。")
        if render_evidence.get("scene_plan_sha256") != plan_sha:
            fail("SCENE.RENDER_PLAN_IDENTITY_MISMATCH", "渲染证据未绑定当前场景计划。")
        if render_evidence.get("policy_sha256") != POLICY_SHA256:
            fail("SCENE.RENDER_POLICY_IDENTITY_MISMATCH", "渲染证据未绑定当前门禁策略。")
        renderer = render_evidence.get("renderer")
        if (not isinstance(renderer, dict) or not str(renderer.get("name") or "").strip()
                or not str(renderer.get("version") or "").strip()):
            unknown("SCENE.RENDERER_IDENTITY_MISSING", "没有记录渲染器名称和版本。")
        for path_key, hash_key, missing_code, mismatch_code, label in (
                ("rendered_pdf_path", "rendered_pdf_sha256",
                 "SCENE.RENDER_PDF_IDENTITY_MISSING", "SCENE.RENDER_PDF_HASH_MISMATCH",
                 "候选渲染 PDF"),
                ("suppressed_rendered_pdf_path", "suppressed_rendered_pdf_sha256",
                 "SCENE.RENDER_SUPPRESSED_PDF_IDENTITY_MISSING",
                 "SCENE.RENDER_SUPPRESSED_PDF_HASH_MISMATCH", "无场景参考渲染 PDF")):
            pdf_path = Path(str(render_evidence.get(path_key) or ""))
            expected_pdf_sha = str(render_evidence.get(hash_key) or "")
            if not pdf_path.is_file() or not re.fullmatch(r"[0-9a-f]{64}", expected_pdf_sha):
                unknown(missing_code, f"没有提供可复核的{label}及其 SHA-256。")
                continue
            actual_pdf_sha = _sha(pdf_path)
            if actual_pdf_sha != expected_pdf_sha:
                fail(mismatch_code, f"{label}文件哈希与渲染证据不一致。",
                     expected=expected_pdf_sha, actual=actual_pdf_sha)

        prs, embedded = _scene_picture_pixel_inputs(candidate_path)
        planned = plan.get("images") or {}
        result["summary"].update({"page_count": len(prs.slides),
                                  "planned_scene_count": len(planned),
                                  "rendered_scene_count": 0,
                                  "tile_grid": _SCENE_RENDER_GRID,
                                  "maximum_tile_mean_absolute_error": _SCENE_RENDER_MAX_TILE_MAE,
                                  "minimum_matching_tile_fraction": _SCENE_RENDER_MIN_MATCHED_TILES,
                                  "minimum_candidate_pixel_contribution_fraction":
                                      _SCENE_RENDER_MIN_CHANGED_PIXEL_FRACTION,
                                  "minimum_source_retained_fraction": _SCENE_RENDER_MIN_SOURCE_RETAINED})
        if not planned:
            fail("SCENE.RENDER_NO_PLANNED_SCENES", "场景像素门禁没有可核验的计划照片。")

        with tempfile.TemporaryDirectory(prefix="wengao-scene-suppressed-") as temp_dir:
            suppressed_pptx = Path(temp_dir) / "candidate-without-scene-pictures.pptx"
            suppressed_identity = make_scene_suppressed_pptx(candidate_path, suppressed_pptx)
            removed_keys = [str(item.get("scene_key") or "")
                            for item in suppressed_identity["removed_scene_pictures"]]
            if (render_evidence.get("scene_suppressed_pptx_sha256")
                    != suppressed_identity["scene_suppressed_pptx_sha256"]):
                fail("SCENE.RENDER_SUPPRESSED_IDENTITY_MISMATCH",
                     "无场景参考 PPTX 不是从当前候选精确移除场景图片得到。",
                     expected=suppressed_identity["scene_suppressed_pptx_sha256"],
                     actual=render_evidence.get("scene_suppressed_pptx_sha256"))
            if Counter(removed_keys) != Counter(str(key) for key in planned):
                fail("SCENE.RENDER_SUPPRESSED_SCOPE_MISMATCH",
                     "无场景参考必须且只能移除冻结计划中的场景图。",
                     expected=sorted(str(key) for key in planned), actual=sorted(removed_keys))
            result["summary"]["scene_suppressed_pptx_sha256"] = \
                suppressed_identity["scene_suppressed_pptx_sha256"]

        page_rows = render_evidence.get("pages")
        page_records = {}
        if not isinstance(page_rows, list):
            unknown("SCENE.RENDER_PAGE_EVIDENCE_MISSING", "逐页 PNG 渲染清单缺失。")
            page_rows = []
        for row in page_rows:
            if not isinstance(row, dict):
                fail("SCENE.RENDER_PAGE_RECORD_INVALID", "逐页渲染记录格式无效。")
                continue
            try:
                page = int(row.get("page"))
            except (TypeError, ValueError):
                fail("SCENE.RENDER_PAGE_RECORD_INVALID", "逐页渲染记录页码无效。")
                continue
            if page in page_records:
                fail("SCENE.RENDER_PAGE_DUPLICATED", "渲染清单重复登记同一页。", slide=page)
            page_records[page] = row
        expected_pages = set(range(1, len(prs.slides) + 1))
        if set(page_records) != expected_pages:
            unknown("SCENE.RENDER_PAGE_SCOPE_INCOMPLETE", "渲染证据必须完整覆盖 PPTX 全页。",
                    expected=sorted(expected_pages), actual=sorted(page_records))
        renders = {}
        for page in sorted(expected_pages & set(page_records)):
            row = page_records[page]
            png_path = Path(str(row.get("png_path") or ""))
            expected_png_sha = str(row.get("png_sha256") or "")
            if not png_path.is_file():
                unknown("SCENE.RENDER_PAGE_FILE_MISSING", "逐页 PNG 文件不存在。", slide=page)
                continue
            actual_png_sha = _sha(png_path)
            if not re.fullmatch(r"[0-9a-f]{64}", expected_png_sha):
                unknown("SCENE.RENDER_PAGE_HASH_MISSING", "逐页 PNG 缺少有效 SHA-256。", slide=page)
                continue
            if actual_png_sha != expected_png_sha:
                fail("SCENE.RENDER_PAGE_HASH_MISMATCH", "逐页 PNG 文件哈希与渲染证据不一致。",
                     slide=page, expected=expected_png_sha, actual=actual_png_sha)
                continue
            try:
                with Image.open(png_path) as im:
                    im.load()
                    page_image = im.convert("RGB")
            except Exception as exc:  # noqa: BLE001
                fail("SCENE.RENDER_PAGE_IMAGE_INVALID", "逐页 PNG 无法解码。",
                     slide=page, error_type=type(exc).__name__)
                continue
            if page_image.width < _SCENE_RENDER_MIN_WIDTH or page_image.height < _SCENE_RENDER_MIN_HEIGHT:
                unknown("SCENE.RENDER_PAGE_RESOLUTION_LOW", "逐页渲染分辨率不足，无法证明照片细节可见。",
                        slide=page, width=page_image.width, height=page_image.height)
                continue
            slide = prs.slides[page - 1]
            slide_aspect = float(prs.slide_width) / max(1.0, float(prs.slide_height))
            png_aspect = float(page_image.width) / max(1.0, float(page_image.height))
            if abs(png_aspect - slide_aspect) / max(0.001, slide_aspect) > 0.015:
                fail("SCENE.RENDER_PAGE_ASPECT_MISMATCH", "渲染页比例与候选 PPTX 不一致。",
                     slide=page, expected=slide_aspect, actual=png_aspect)
                continue
            suppressed_path = Path(str(row.get("suppressed_png_path") or ""))
            expected_suppressed_sha = str(row.get("suppressed_png_sha256") or "")
            if not suppressed_path.is_file():
                unknown("SCENE.RENDER_SUPPRESSED_PAGE_FILE_MISSING",
                        "无场景参考逐页 PNG 文件不存在。", slide=page)
                continue
            actual_suppressed_sha = _sha(suppressed_path)
            if not re.fullmatch(r"[0-9a-f]{64}", expected_suppressed_sha):
                unknown("SCENE.RENDER_SUPPRESSED_PAGE_HASH_MISSING",
                        "无场景参考逐页 PNG 缺少有效 SHA-256。", slide=page)
                continue
            if actual_suppressed_sha != expected_suppressed_sha:
                fail("SCENE.RENDER_SUPPRESSED_PAGE_HASH_MISMATCH",
                     "无场景参考逐页 PNG 哈希与渲染证据不一致。", slide=page,
                     expected=expected_suppressed_sha, actual=actual_suppressed_sha)
                continue
            try:
                with Image.open(suppressed_path) as im:
                    im.load()
                    suppressed_image = im.convert("RGB")
            except Exception as exc:  # noqa: BLE001
                fail("SCENE.RENDER_SUPPRESSED_IMAGE_INVALID",
                     "无场景参考逐页 PNG 无法解码。", slide=page,
                     error_type=type(exc).__name__)
                continue
            if suppressed_image.size != page_image.size:
                fail("SCENE.RENDER_SUPPRESSED_DIMENSIONS_MISMATCH",
                     "候选渲染与无场景参考渲染像素尺寸不一致。", slide=page,
                     candidate_size=page_image.size, suppressed_size=suppressed_image.size)
                continue
            suppressed_aspect = float(suppressed_image.width) / max(1.0, float(suppressed_image.height))
            if abs(suppressed_aspect - slide_aspect) / max(0.001, slide_aspect) > 0.015:
                fail("SCENE.RENDER_SUPPRESSED_ASPECT_MISMATCH",
                     "无场景参考渲染页比例与候选 PPTX 不一致。", slide=page,
                     expected=slide_aspect, actual=suppressed_aspect)
                continue
            renders[page] = (page_image, suppressed_image)

        visibility = {}
        for scene_key, spec in planned.items():
            try:
                page = int(spec.get("page"))
            except (TypeError, ValueError):
                fail("SCENE.RENDER_PLACEMENT_INVALID", "图片计划页码无效。", scene_key=scene_key)
                continue
            markers = embedded.get(str(scene_key), [])
            if len(markers) != 1:
                fail("SCENE.RENDER_MARKER_COVERAGE_MISMATCH",
                     "候选 PPTX 中每个场景必须恰有一个可核验的图片对象。",
                     slide=page, scene_key=scene_key, actual_marker_count=len(markers))
                continue
            marker = markers[0]
            if marker["page"] != page:
                fail("SCENE.RENDER_PICTURE_PAGE_MISMATCH", "嵌入场景图页码与计划不一致。",
                     scene_key=scene_key, expected=page, actual=marker["page"])
                continue
            expected_image_sha = str(spec.get("sha256") or "")
            if (marker["actual_sha256"] != expected_image_sha
                    or marker["marker_sha256"] != expected_image_sha):
                fail("SCENE.RENDER_PICTURE_IDENTITY_MISMATCH", "像素检查中的图片不是计划绑定的嵌入字节。",
                     slide=page, scene_key=scene_key,
                     expected=expected_image_sha, actual=marker["actual_sha256"])
                continue
            if page not in renders:
                unknown("SCENE.RENDER_SCENE_PAGE_UNAVAILABLE", "场景所在页缺少有效全页渲染。",
                        slide=page, scene_key=scene_key)
                continue
            try:
                left, top, width, height = marker["emu_rect"]
                right, bottom = left + width, top + height
                if width <= 0 or height <= 0 or left < 0 or top < 0 \
                        or right > prs.slide_width or bottom > prs.slide_height:
                    raise ValueError("scene rectangle is outside the slide")
                page_image, suppressed_page_image = renders[page]
                x0 = max(0, int(math.floor(left * page_image.width / prs.slide_width)))
                y0 = max(0, int(math.floor(top * page_image.height / prs.slide_height)))
                x1 = min(page_image.width, int(math.ceil(right * page_image.width / prs.slide_width)))
                y1 = min(page_image.height, int(math.ceil(bottom * page_image.height / prs.slide_height)))
                if x1 - x0 < 32 or y1 - y0 < 32:
                    raise ValueError("scene rectangle is too small in the rendered image")
                crop = marker["crop"]
                if (len(crop) != 4 or any(not math.isfinite(value) or value < -0.0001 or value >= 1
                                          for value in crop)):
                    raise ValueError("invalid scene crop fractions")
                crop = [max(0.0, value) for value in crop]
                crop_left, crop_top, crop_right, crop_bottom = crop
                retained = max(0.0, 1.0 - crop_left - crop_right) * \
                    max(0.0, 1.0 - crop_top - crop_bottom)
                if retained < _SCENE_RENDER_MIN_SOURCE_RETAINED:
                    fail("SCENE.RENDER_SOURCE_CONTENT_CROPPED",
                         "实际 OOXML 裁切保留的原图内容低于 HG13 可见性下限。",
                         slide=page, scene_key=scene_key,
                         source_retained_fraction=round(retained, 5),
                         required_fraction=_SCENE_RENDER_MIN_SOURCE_RETAINED)
                    continue
                source = Image.open(io.BytesIO(marker["image_blob"])).convert("RGB")
                sw, sh = source.size
                sx0 = int(round(crop_left * sw))
                sy0 = int(round(crop_top * sh))
                sx1 = int(round((1.0 - crop_right) * sw))
                sy1 = int(round((1.0 - crop_bottom) * sh))
                if sx1 <= sx0 or sy1 <= sy0:
                    raise ValueError("crop removes all source image pixels")
                source = source.crop((sx0, sy0, sx1, sy1))
                roi = page_image.crop((x0, y0, x1, y1))
                expected = source.resize(roi.size, Image.Resampling.LANCZOS)
                match_ratio, matched_tiles, tile_count, mean_mae = _tile_match_ratio(roi, expected)
                suppressed_roi = suppressed_page_image.crop((x0, y0, x1, y1))
                contribution = ImageChops.difference(roi, suppressed_roi).convert("L")
                contribution_mask = contribution.point(lambda value: 255 if value >= 18 else 0)
                contribution_fraction = (contribution_mask.histogram()[255]
                                         / max(1, roi.width * roi.height))
                visible = (match_ratio >= _SCENE_RENDER_MIN_MATCHED_TILES
                           and contribution_fraction >= _SCENE_RENDER_MIN_CHANGED_PIXEL_FRACTION)
                visibility[str(scene_key)] = {
                    "slide": page,
                    "matched_tile_fraction": round(match_ratio, 5),
                    "matched_tiles": matched_tiles,
                    "tile_count": tile_count,
                    "mean_tile_mae": round(mean_mae, 3),
                    "candidate_pixel_contribution_fraction": round(contribution_fraction, 5),
                    "source_retained_fraction": round(retained, 5),
                    "visible": visible,
                }
                result["summary"]["rendered_scene_count"] += 1
                if not visible:
                    fail("SCENE.RENDERED_IMAGE_NOT_VISIBLE",
                         "候选照片没有在真实渲染中形成足够像素贡献，或渲染区与实际嵌入照片不匹配。",
                         slide=page, scene_key=scene_key,
                         matched_tile_fraction=round(match_ratio, 5),
                         required_fraction=_SCENE_RENDER_MIN_MATCHED_TILES,
                         candidate_pixel_contribution_fraction=round(contribution_fraction, 5),
                         required_contribution_fraction=_SCENE_RENDER_MIN_CHANGED_PIXEL_FRACTION,
                         mean_tile_mae=round(mean_mae, 3))
            except Exception as exc:  # noqa: BLE001 — invalid pixel evidence never passes
                fail("SCENE.RENDER_VISIBILITY_CHECK_FAILED", "无法完成场景图像素可见性核验。",
                     slide=page, scene_key=scene_key, error_type=type(exc).__name__)
        result["summary"]["scenes"] = visibility
    except Exception as exc:  # noqa: BLE001 — missing or malformed evidence is never a pass
        if not findings and not unverified:
            unknown("SCENE.RENDER_CHECK_NOT_EVALUABLE",
                    f"HG13 像素门禁缺少可复核证据：{type(exc).__name__}。")
    result["status"] = "FAILED" if findings else "NOT_EVALUABLE" if unverified else "EXECUTED"
    result["blocking"] = result["status"] != "EXECUTED"
    return result


def validate_complete_output(source_pptx: str | Path, candidate_pptx: str | Path,
                             plan_path: str | Path | None,
                             render_evidence: dict | None,
                             asset_dir: str | Path | None = None, *,
                             require_visual_review: bool = True) -> dict:
    """Combine structural scene validation with the required rendered-pixel gate."""
    structural = validate_output(source_pptx, candidate_pptx, plan_path, asset_dir,
                                 require_visual_review=require_visual_review)
    pixels = validate_rendered_scene_visibility(candidate_pptx, plan_path, render_evidence)
    coverage_audit = _validate_coverage_audit_proof(source_pptx, plan_path)
    statuses = {structural.get("status"), pixels.get("status"),
                coverage_audit.get("status")}
    status = ("FAILED" if "FAILED" in statuses else
              "EXECUTED" if statuses == {"EXECUTED"} else "NOT_EVALUABLE")
    findings = []
    findings.extend({**item, "origin": "semantic_scene_structure"}
                    for item in structural.get("findings") or [])
    findings.extend({**item, "origin": "semantic_scene_pixels"}
                    for item in pixels.get("findings") or [])
    findings.extend({**item, "origin": "semantic_scene_coverage_audit"}
                    for item in coverage_audit.get("findings") or [])
    unverified = list(pixels.get("unverified") or [])
    unverified.extend(coverage_audit.get("unverified") or [])
    if structural.get("status") == "NOT_EVALUABLE":
        unverified.append({"code": "SCENE.STRUCTURE_NOT_EVALUABLE",
                           "detail": "结构/来源场景检查未执行完成。"})
    return {"schema_version": "WG-COMPLETE-SEMANTIC-SCENE-GATE/1",
            "gate_id": SCENE_GATE_ID, "status": status,
            "blocking": status != "EXECUTED", "findings": findings,
            "unverified": unverified,
            "summary": {"structural": structural.get("summary") or {},
                        "rendered_pixel_visibility": pixels.get("summary") or {},
                        "coverage_audit": coverage_audit.get("summary") or {},
                        "structural_status": structural.get("status"),
                        "pixel_status": pixels.get("status"),
                        "coverage_audit_status": coverage_audit.get("status")}}


def _validate_coverage_audit_proof(source_pptx: str | Path,
                                   plan_path: str | Path | None) -> dict:
    """Verify that a real second-pass result is bound to this source and plan.

    Caller-injected semantic fixtures may exercise structural and pixel helpers,
    but they cannot satisfy the production completion gate.
    """
    result = {"status": "NOT_EVALUABLE", "findings": [], "unverified": [],
              "summary": {}}
    if not plan_path or not Path(plan_path).is_file() or not Path(source_pptx).is_file():
        result["unverified"].append({
            "code": "SCENE.COVERAGE_AUDIT_EVIDENCE_MISSING",
            "detail": "完整场景门禁缺少来源稿或冻结场景计划，无法验证第二轮语义复核。"})
        return result
    try:
        plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
        source_pages = _source_pages(source_pptx)
        proof = plan.get("coverage_audit")
        if not isinstance(proof, dict) or proof.get("status") != "PASSED":
            result["unverified"].append({
                "code": "SCENE.COVERAGE_AUDIT_UNVERIFIED",
                "detail": "语义场景计划没有真实第二轮逐页复核凭据；注入样例不能替代模型审计。"})
            return result
        hex64 = re.compile(r"^[0-9a-f]{64}$")
        required_hashes = ("response_sha256", "prompt_sha256", "result_sha256")
        if any(not hex64.fullmatch(str(proof.get(name) or ""))
               for name in required_hashes):
            result["findings"].append({
                "code": "SCENE.COVERAGE_AUDIT_HASH_MISSING",
                "severity": "BLOCKING",
                "detail": "第二轮逐页复核缺少完整响应、提示与结果哈希。"})
            result["status"] = "FAILED"
            return result
        if int(proof.get("pages_checked", -1)) != len(source_pages):
            result["findings"].append({
                "code": "SCENE.COVERAGE_AUDIT_PAGE_COUNT_MISMATCH",
                "severity": "BLOCKING",
                "detail": "第二轮语义复核页数与来源稿不一致。"})
        audit_result = proof.get("result")
        result_hash = hashlib.sha256(json.dumps(
            audit_result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest() if isinstance(audit_result, dict) else ""
        if result_hash != proof.get("result_sha256"):
            result["findings"].append({
                "code": "SCENE.COVERAGE_AUDIT_RESULT_HASH_MISMATCH",
                "severity": "BLOCKING",
                "detail": "逐页复核结果哈希与冻结内容不一致。"})
        phase = proof.get("phase", "second_pass")
        # "post_empty_plan_reconsideration" is scene_model.py's third legitimate
        # phase (see _plan_scenes_in_bounded_batches's empty_scene_recovery): a
        # first planner pass that comes back with zero scenes gets exactly one
        # same-model, source-grounded reconsideration, then the ordinary full
        # coverage audit — built and hashed with scene_coverage_audit_prompt's
        # own "post_repair" wording, never a fabricated scene. This validator
        # had never been told about that phase, so a legitimately re-verified
        # empty-plan recovery was rejected as if its phase marker were garbage.
        if phase not in ("second_pass", "post_repair", "post_empty_plan_reconsideration"):
            result["findings"].append({
                "code": "SCENE.COVERAGE_AUDIT_PHASE_INVALID",
                "severity": "BLOCKING",
                "detail": "逐页复核阶段标记无效。"})
            result["status"] = "FAILED"
            return result
        # 提示词按复核当时冻结的计划视图重放；该视图必须与最终计划一致
        # （仅允许确定性的 canonical_scene_id 规范化），否则视为未绑定。
        audited_view = proof.get("audited_plan_view")
        prompt_plan = plan
        if isinstance(audited_view, dict):
            drift = _audited_plan_view_drift(audited_view, plan)
            if drift:
                result["findings"].append({
                    "code": "SCENE.COVERAGE_AUDIT_PLAN_DRIFT",
                    "severity": "BLOCKING",
                    "detail": "复核所依据的计划与最终场景计划不一致：" + ", ".join(drift[:8])})
            prompt_plan = audited_view
        # scene_model.py builds the empty-plan-reconsideration's verification
        # audit with phase="post_repair" wording too (it is, structurally, a
        # post-repair-style full re-audit of a replanned deck) — replay it the
        # same way or the hash can never match.
        audit_prompt = scene_coverage_audit_prompt(
            source_pages, prompt_plan,
            phase="post_repair" if phase in ("post_repair", "post_empty_plan_reconsideration")
            else "second_pass")
        prompt_hash = hashlib.sha256(audit_prompt.encode("utf-8")).hexdigest()
        if prompt_hash != proof.get("prompt_sha256"):
            result["findings"].append({
                "code": "SCENE.COVERAGE_AUDIT_PROMPT_HASH_MISMATCH",
                "severity": "BLOCKING",
                "detail": "逐页复核提示未绑定当前来源页与冻结场景计划。"})
        if isinstance(audit_result, dict):
            try:
                summary = _validate_scene_coverage_audit(audit_result, source_pages, plan)
                if (summary.get("pages_checked") != proof.get("pages_checked")
                        or summary.get("missing_scenes") != proof.get("missing_scenes")
                        or summary.get("merged_distinct_scenes") != proof.get("merged_distinct_scenes")):
                    result["findings"].append({
                        "code": "SCENE.COVERAGE_AUDIT_SUMMARY_MISMATCH",
                        "severity": "BLOCKING",
                        "detail": "复核汇总与逐页原始结果不一致。"})
            except SceneModelError as exc:
                result["findings"].append({"code": exc.code, "severity": "BLOCKING",
                                           "detail": exc.detail})
        else:
            result["findings"].append({
                "code": "SCENE.COVERAGE_AUDIT_RESULT_MISSING",
                "severity": "BLOCKING",
                "detail": "冻结计划中缺少逐页复核原始结果。"})
        repair = proof.get("gap_repair")
        if repair is not None:
            try:
                if (phase != "post_repair" or not isinstance(repair, dict)
                        or repair.get("status") != "APPLIED_AND_REVERIFIED"):
                    raise ValueError("repair state is not post-repair verified")
                before_plan = repair.get("before_plan")
                if not isinstance(before_plan, dict):
                    raise ValueError("pre-repair plan missing")
                if _repair_binding_sha256(before_plan) != repair.get("before_plan_sha256"):
                    raise ValueError("pre-repair plan hash mismatch")
                gap_result = repair.get("result")
                gap_result_sha = hashlib.sha256(json.dumps(
                    gap_result, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":")).encode("utf-8")).hexdigest()
                if gap_result_sha != repair.get("result_sha256"):
                    raise ValueError("pre-repair audit result hash mismatch")
                gap_prompt = scene_coverage_audit_prompt(source_pages, before_plan)
                if hashlib.sha256(gap_prompt.encode("utf-8")).hexdigest() != repair.get("prompt_sha256"):
                    raise ValueError("pre-repair audit prompt hash mismatch")
                if not all(hex64.fullmatch(str(repair.get(key) or ""))
                           for key in ("response_sha256", "prompt_sha256", "result_sha256")):
                    raise ValueError("pre-repair audit hash missing")
                initial_summary = _validate_scene_coverage_audit(
                    gap_result, source_pages, before_plan, allow_gaps=True)
                repair_kind = repair.get("kind", "grounded_gap_repair")
                if repair_kind == "grounded_gap_repair":
                    if (initial_summary.get("status") != "GAPS_FOUND"
                            or initial_summary.get("missing_scenes", 0) < 1
                            or initial_summary.get("merged_distinct_scenes", 0) != 0):
                        raise ValueError("pre-repair audit was not a repairable grounded gap")
                    replay_gap_result = gap_result
                    repair_payload = repair.get("gap_repair_result")
                    if repair_payload is not None:
                        gap_repair_prompt = scene_gap_repair_prompt(
                            source_pages, before_plan, gap_result)
                        if (hashlib.sha256(gap_repair_prompt.encode("utf-8")).hexdigest()
                                != repair.get("gap_repair_prompt_sha256")):
                            raise ValueError("gap-repair prompt is not bound to source pages and audit findings")
                        if not hex64.fullmatch(str(repair.get("gap_repair_response_sha256") or "")):
                            raise ValueError("gap-repair response hash missing")
                        repair_payload_sha = hashlib.sha256(json.dumps(
                            repair_payload, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")).encode("utf-8")).hexdigest()
                        if repair_payload_sha != repair.get("gap_repair_result_sha256"):
                            raise ValueError("gap-repair result hash mismatch")
                        normalized_repair = _validate_scene_gap_repair_scope(
                            gap_result, repair_payload)
                        replay_gap_result = dict(gap_result)
                        replay_gap_result["missing_scenes"] = normalized_repair["missing_scenes"]
                    replayed_repair = _apply_grounded_scene_gap_repairs(
                        replay_gap_result, source_pages, before_plan)
                    reconstructed = replayed_repair["plan"]
                    _enrich_scene_image_prompts(reconstructed, source_pages)
                    repaired_sha = _repair_binding_sha256(reconstructed)
                    if (repaired_sha != repair.get("repaired_plan_sha256")
                            or repaired_sha != _repair_binding_sha256(plan)
                            or replayed_repair.get("repaired_scene_keys") != repair.get("repaired_scene_keys")):
                        raise ValueError("repaired scene ledger does not match frozen final plan")
                    if (repair.get("repaired_scene_count") != len(gap_result.get("missing_scenes") or [])
                            or not isinstance(repair.get("repaired_scene_keys"), list)
                            or len(repair["repaired_scene_keys"]) != repair.get("repaired_scene_count")):
                        raise ValueError("repair delta count mismatch")
                elif repair_kind == "merge_replan_patches":
                    if (initial_summary.get("status") != "GAPS_FOUND"
                            or initial_summary.get("merged_distinct_scenes", 0) < 1):
                        raise ValueError("pre-repair audit did not identify a distinct-scene merge")
                    replanned = repair.get("replanned_plan")
                    replan_meta = repair.get("replan")
                    components = _scene_merge_conflict_components(gap_result)
                    traces = replan_meta.get("components") if isinstance(replan_meta, dict) else None
                    if (not isinstance(replanned, dict) or not isinstance(replan_meta, dict)
                            or replan_meta.get("planner_mode") != "audited_conflict_page_patches"
                            or not isinstance(traces, list) or len(traces) != len(components)
                            or replan_meta.get("component_count") != len(components)):
                        raise ValueError("page-scoped conflict patch evidence missing")

                    working = _repair_binding_payload(before_plan)
                    response_hashes = []
                    for component, trace in zip(components, traces):
                        if (not isinstance(trace, dict)
                                or trace.get("pages") != component["pages"]
                                or trace.get("scene_keys") != component["scene_keys"]
                                or trace.get("finding_count") != len(component["findings"])):
                            raise ValueError("conflict patch scope does not match audited component")
                        component_audit = {"merged_distinct_scenes": component["findings"]}
                        patch_prompt = scene_merge_replan_prompt(
                            source_pages, working, component_audit)
                        if hashlib.sha256(patch_prompt.encode("utf-8")).hexdigest() != trace.get("prompt_sha256"):
                            raise ValueError("conflict patch prompt is not bound to source pages and audit")
                        patch = trace.get("patch_result")
                        patch_sha = hashlib.sha256(json.dumps(
                            patch, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")).encode("utf-8")).hexdigest()
                        if (patch_sha != trace.get("patch_result_sha256")
                                or not hex64.fullmatch(str(trace.get("response_sha256") or ""))):
                            raise ValueError("conflict patch result or response hash mismatch")
                        working = _merge_scene_conflict_patch(
                            working, patch, source_pages, component)
                        response_hashes.append(trace.get("response_sha256"))
                    aggregate_response_sha = hashlib.sha256(json.dumps(
                        response_hashes, separators=(",", ":")).encode("utf-8")).hexdigest()
                    if (aggregate_response_sha != replan_meta.get("response_sha256")
                            or aggregate_response_sha != repair.get("replan_response_sha256")):
                        raise ValueError("aggregate conflict patch response hash mismatch")
                    _enrich_scene_image_prompts(working, source_pages)
                    replayed_keys = _validate_scene_merge_replan_scope(
                        before_plan, working, source_pages, gap_result)
                    merge_sha = _repair_binding_sha256(working)
                    if (merge_sha != repair.get("replanned_plan_sha256")
                            or merge_sha != repair.get("replan_result_sha256")
                            or merge_sha != _repair_binding_sha256(replanned)):
                        raise ValueError("merged conflict patch ledger hash mismatch")
                    if (not isinstance(repair.get("repaired_scene_keys"), list)
                            or replayed_keys != repair.get("repaired_scene_keys")
                            or repair.get("repaired_scene_count") != len(replayed_keys)):
                        raise ValueError("merge patch delta count mismatch")

                    concurrent_gap = repair.get("concurrent_gap_repair")
                    missing_scenes = gap_result.get("missing_scenes") or []
                    if missing_scenes:
                        if not isinstance(concurrent_gap, dict):
                            raise ValueError("coexisting source-grounded gaps were not repaired")
                        gap_audit = json.loads(json.dumps(gap_result, ensure_ascii=False))
                        gap_audit["merged_distinct_scenes"] = []
                        gap_audit["review_status"] = "GAPS_FOUND"
                        coverage_by_page = {int(row["page"]): row
                                            for row in working.get("page_coverage", [])}
                        gap_audit["page_reviews"] = [{
                            "page": int(page["page"]),
                            "coverage_status": ("SCENE_COVERED"
                                                if coverage_by_page[int(page["page"])].get("scene_keys")
                                                else "NO_SCENE"),
                            "scene_keys": list(coverage_by_page[int(page["page"])].get("scene_keys") or []),
                        } for page in source_pages]
                        gap_payload = concurrent_gap.get("gap_repair_result")
                        gap_audit_for_apply = gap_audit
                        if gap_payload is not None:
                            gap_prompt = scene_gap_repair_prompt(source_pages, working, gap_audit)
                            if (hashlib.sha256(gap_prompt.encode("utf-8")).hexdigest()
                                    != concurrent_gap.get("gap_repair_prompt_sha256")):
                                raise ValueError("concurrent gap repair prompt hash mismatch")
                            gap_payload_sha = hashlib.sha256(json.dumps(
                                gap_payload, ensure_ascii=False, sort_keys=True,
                                separators=(",", ":")).encode("utf-8")).hexdigest()
                            if (gap_payload_sha != concurrent_gap.get("gap_repair_result_sha256")
                                    or not hex64.fullmatch(str(
                                        concurrent_gap.get("gap_repair_response_sha256") or ""))):
                                raise ValueError("concurrent gap repair response evidence mismatch")
                            normalized_gap = _validate_scene_gap_repair_scope(gap_audit, gap_payload)
                            gap_audit_for_apply = json.loads(json.dumps(gap_audit, ensure_ascii=False))
                            gap_audit_for_apply["missing_scenes"] = normalized_gap["missing_scenes"]
                        gap_applied = _apply_grounded_scene_gap_repairs(
                            gap_audit_for_apply, source_pages, working)
                        if gap_applied.get("repaired_scene_keys") != concurrent_gap.get(
                                "repaired_gap_scene_keys"):
                            raise ValueError("concurrent gap repair delta mismatch")
                        reconstructed = gap_applied["plan"]
                    else:
                        if concurrent_gap is not None:
                            raise ValueError("unexpected gap repair proof without audited gaps")
                        reconstructed = working
                    _enrich_scene_image_prompts(reconstructed, source_pages)
                    repaired_sha = _repair_binding_sha256(reconstructed)
                    if (repaired_sha != repair.get("repaired_plan_sha256")
                            or repaired_sha != _repair_binding_sha256(plan)):
                        raise ValueError("repaired patch ledger does not match the frozen final scene plan")
                elif repair_kind == "merge_replan":
                    if (initial_summary.get("status") != "GAPS_FOUND"
                            or initial_summary.get("merged_distinct_scenes", 0) < 1):
                        raise ValueError("pre-repair audit did not identify a distinct-scene merge")
                    replanned = repair.get("replanned_plan")
                    if not isinstance(replanned, dict):
                        raise ValueError("source-bounded replan missing")
                    replanned_sha = _repair_binding_sha256(replanned)
                    replan_result_sha = hashlib.sha256(json.dumps(
                        replanned, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":")).encode("utf-8")).hexdigest()
                    if (replanned_sha != repair.get("replanned_plan_sha256")
                            or replan_result_sha != repair.get("replan_result_sha256")):
                        raise ValueError("replanned plan hash mismatch")
                    replan_prompt = scene_merge_replan_prompt(
                        source_pages, before_plan, gap_result)
                    if hashlib.sha256(replan_prompt.encode("utf-8")).hexdigest() != repair.get("replan_prompt_sha256"):
                        raise ValueError("replan prompt is not bound to the source pages and merge audit")
                    if not hex64.fullmatch(str(repair.get("replan_response_sha256") or "")):
                        raise ValueError("replan response hash missing")
                    replayed_keys = _validate_scene_merge_replan_scope(
                        before_plan, replanned, source_pages, gap_result)
                    if (not isinstance(repair.get("repaired_scene_keys"), list)
                            or replayed_keys != repair.get("repaired_scene_keys")
                            or repair.get("repaired_scene_count") != len(replayed_keys)):
                        raise ValueError("merge-replan delta mismatch")
                    repaired_scenes, repaired_coverage = _normalize_semantics(
                        replanned, source_pages, require_canonical_scene_id=True)
                    reconstructed = dict(replanned)
                    reconstructed["scenes"] = repaired_scenes
                    reconstructed["page_coverage"] = repaired_coverage
                    _enrich_scene_image_prompts(reconstructed, source_pages)
                    repaired_sha = _repair_binding_sha256(reconstructed)
                    if (repaired_sha != repair.get("repaired_plan_sha256")
                            or repaired_sha != _repair_binding_sha256(plan)):
                        raise ValueError("replanned ledger does not match the frozen final scene plan")
                else:
                    raise ValueError("unknown scene repair kind")
            except Exception as exc:
                result["findings"].append({
                    "code": "SCENE.COVERAGE_AUDIT_REPAIR_PROOF_INVALID",
                    "severity": "BLOCKING",
                    "detail": f"审计授权的场景修复无法重现：{type(exc).__name__}。"})
        elif phase == "post_repair":
            result["findings"].append({
                "code": "SCENE.COVERAGE_AUDIT_REPAIR_PROOF_MISSING",
                "severity": "BLOCKING",
                "detail": "修复后复核没有绑定其原始漏项审计与账本变更。"})
        result["summary"] = {"pages_checked": proof.get("pages_checked"),
                             "response_sha256": proof.get("response_sha256"),
                             "prompt_sha256": proof.get("prompt_sha256"),
                             "result_sha256": proof.get("result_sha256")}
        result["status"] = "FAILED" if result["findings"] else "EXECUTED"
    except Exception as exc:  # evidence errors are blocking, never an implicit pass
        result["unverified"].append({
            "code": "SCENE.COVERAGE_AUDIT_EVIDENCE_INVALID",
            "detail": f"第二轮语义复核凭据无法完整读取：{type(exc).__name__}。"})
    return result


def _validate_page_coverage(plan: dict, source_pages: list[dict], findings: list[dict]) -> None:
    rows = plan.get("page_coverage")
    if not isinstance(rows, list):
        findings.append({"code": "SCENE.PAGE_COVERAGE_MISSING", "severity": "BLOCKING",
                         "detail": "场景计划缺少逐页覆盖账本。"})
        return
    by_page = {}
    for row in rows:
        if not isinstance(row, dict):
            findings.append({"code": "SCENE.PAGE_COVERAGE_INVALID", "severity": "BLOCKING",
                             "detail": "逐页覆盖账本每一行都必须是对象。"})
            continue
        try:
            page = int(row.get("page"))
        except (AttributeError, TypeError, ValueError):
            findings.append({"code": "SCENE.PAGE_COVERAGE_INVALID", "severity": "BLOCKING",
                             "detail": "逐页覆盖账本存在无效页码。"})
            continue
        if page in by_page:
            findings.append({"code": "SCENE.PAGE_COVERAGE_DUPLICATED", "severity": "BLOCKING",
                             "slide": page, "detail": "逐页覆盖账本重复登记。"})
        by_page[page] = row
    expected_pages = {int(p["page"]): p for p in source_pages}
    if set(by_page) != set(expected_pages):
        findings.append({"code": "SCENE.PAGE_COVERAGE_GAP", "severity": "BLOCKING",
                         "expected": sorted(expected_pages), "actual": sorted(by_page),
                         "detail": "逐页覆盖账本必须与原稿页数完全一致。"})
    scenes = {str(s.get("scene_key")): s for s in plan.get("scenes") or []}
    for page, row in by_page.items():
        if page not in expected_pages:
            continue
        keys = row.get("scene_keys")
        if not isinstance(keys, list):
            findings.append({"code": "SCENE.PAGE_COVERAGE_INVALID", "severity": "BLOCKING",
                             "slide": page, "detail": "逐页账本的 scene_keys 必须是数组。"})
            keys = []
        if any(not isinstance(key, str) or not _KEY_RE.fullmatch(key)
               for key in keys):
            findings.append({"code": "SCENE.PAGE_COVERAGE_INVALID", "severity": "BLOCKING",
                             "slide": page, "detail": "逐页账本包含无效场景键。"})
        if len(keys) != len(set(key for key in keys if isinstance(key, str))):
            findings.append({"code": "SCENE.PAGE_COVERAGE_DUPLICATED", "severity": "BLOCKING",
                             "slide": page, "detail": "逐页账本重复登记了同一场景键。"})
        reason = str(row.get("no_scene_reason") or "").strip()
        if keys and reason:
            findings.append({"code": "SCENE.PAGE_COVERAGE_CONFLICT", "severity": "BLOCKING",
                             "slide": page, "detail": "页面同时声明具体场景和无场景理由。"})
        if not keys and not reason:
            findings.append({"code": "SCENE.PAGE_COVERAGE_GAP", "severity": "BLOCKING",
                             "slide": page, "detail": "页面既没有相关场景，也没有有理由的视觉说明。"})
        if not keys and reason and not _is_explicit_no_scene_reason(reason):
            findings.append({"code": "SCENE.PAGE_NO_SCENE_REASON_INVALID", "severity": "BLOCKING",
                             "slide": page, "detail": "无场景理由过于笼统，不能证明已检查页面语义。"})
        if expected_pages[page]["text_chars"] > 0:
            evidence = row.get("evidence") or []
            if not evidence or not all(_excerpt_matches(expected_pages[page]["text"], str(x))
                                       for x in evidence):
                findings.append({"code": "SCENE.PAGE_EVIDENCE_NOT_GROUNDED", "severity": "BLOCKING",
                                 "slide": page, "detail": "逐页场景账本缺少可核对的本页原文片段。"})
        for key in keys:
            scene = scenes.get(str(key))
            if not scene or page not in [int(x) for x in scene.get("candidate_pages", [])]:
                findings.append({"code": "SCENE.PAGE_SCENE_LINK_INVALID", "severity": "BLOCKING",
                                 "slide": page, "scene_key": key,
                                 "detail": "页面场景引用没有对应的来源候选页。"})
        for scene in scenes.values():
            if page not in [int(x) for x in scene.get("candidate_pages", [])]:
                continue
            candidates = [c for c in scene.get("page_candidates", []) if int(c.get("page", 0)) == page]
            if not candidates or scene.get("scene_key") not in keys:
                findings.append({"code": "SCENE.PAGE_SCENE_LINK_INVALID", "severity": "BLOCKING",
                                 "slide": page, "scene_key": scene.get("scene_key"),
                                 "detail": "模型找到的场景候选页没有登记到逐页覆盖账本。"})
            elif not all(_excerpt_matches(expected_pages[page]["text"], str(excerpt))
                         for excerpt in candidates[0].get("evidence", [])):
                findings.append({"code": "SCENE.EVIDENCE_NOT_GROUNDED", "severity": "BLOCKING",
                                 "slide": page, "scene_key": scene.get("scene_key"),
                                 "detail": "冻结计划中的原文证据无法在当前输入中复现。"})


def _candidate_blockers(candidate_pptx: str | Path, spec: dict) -> tuple[float, list[str]]:
    prs = Presentation(str(candidate_pptx))
    page = int(spec["page"])
    if not 1 <= page <= len(prs.slides):
        return 1.0, ["page_out_of_range"]
    slide = prs.slides[page - 1]
    rect = tuple(float(x) for x in spec["rect"])
    overlap, opaque_text = _text_overlap(rect, slide)
    blockers = []
    slide_area = prs.slide_width * prs.slide_height / (_IN * _IN)
    slide_h = prs.slide_height / _IN
    for shape in slide.shapes:
        if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE:
            if shape._element.nvPicPr.cNvPr.get("name", "").startswith("WENGAO_SCENE|"):
                if str(shape._element.nvPicPr.cNvPr.get("name")).split("|")[1] == spec["scene_key"]:
                    continue
        if _rect_intersection_area(rect, _shape_rect(shape)) <= 0:
            continue
        if spec.get("placement_mode") == "replace_visual_image" and _is_background_shape(shape, slide_area, slide_height=slide_h):
            continue
        if getattr(shape, "has_text_frame", False) and (shape.text_frame.text or "").strip():
            # A background scene may sit behind transparent live text, but never under an opaque text card.
            if spec.get("placement_mode") != "replace_visual_image" or _has_opaque_fill(shape):
                blockers.append("text_or_card_overlap")
        elif getattr(shape, "has_table", False) or getattr(shape, "has_chart", False):
            blockers.append("table_or_chart_overlap")
        elif getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE:
            blockers.append("picture_overlap")
        elif _is_large_filled_shape(shape, slide_area):
            target_r = spec.get("target_rect", rect)
            same_slot_mask = (spec.get("placement_mode") == "replace_visual_image"
                              and (_same_rect(_shape_rect(shape), rect)
                                   or _same_rect(_shape_rect(shape), target_r)
                                   or (_rect_intersection_area(_shape_rect(shape), target_r) / max(0.001, _rect_area(_shape_rect(shape))) > 0.85)))
            if not same_slot_mask:
                blockers.append("filled_shape_or_card_overlap")
    return overlap, sorted(set(blockers))


def validate_output(source_pptx: str | Path, candidate_pptx: str | Path,
                    plan_path: str | Path | None, asset_dir: str | Path | None = None, *,
                    require_visual_review: bool = False) -> dict:
    """Validate a frozen plan and, for production, its local visual-review proof."""
    result = {"schema_version": "2.2.0", "gate_id": SCENE_GATE_ID,
              "status": "NOT_EVALUABLE", "findings": [], "summary": {}}
    findings = result["findings"]
    markers = []
    try:
        max_replacement_aspect_factor = _replacement_aspect_limit()
        if _sha(POLICY_FILE) != POLICY_SHA256:
            findings.append({"code": "SCENE.POLICY_IDENTITY_MISMATCH", "severity": "BLOCKING",
                             "detail": "HG13 策略文件哈希与代码冻结身份不一致。"})
        if not Path(source_pptx).is_file() or not Path(candidate_pptx).is_file():
            findings.append({"code": "SCENE.PPTX_MISSING", "severity": "BLOCKING",
                             "detail": "来源或候选 PPTX 不存在。"})
            raise ValueError("source or candidate PPTX missing")
        source_pages = _source_pages(source_pptx)
        candidate = Presentation(str(candidate_pptx))
        result["summary"]["source_page_count"] = len(source_pages)
        if len(candidate.slides) != len(source_pages):
            findings.append({"code": "SCENE.PAGE_COUNT_MISMATCH", "severity": "BLOCKING",
                             "expected": len(source_pages), "actual": len(candidate.slides),
                             "detail": "场景图插入不能改变演示文稿页数。"})
        if not plan_path or not Path(plan_path).is_file():
            findings.append({"code": "SCENE.PLAN_MISSING", "severity": "BLOCKING",
                             "detail": "缺少冻结的语义场景计划。"})
            raise ValueError("scene plan missing")
        plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
        if (require_visual_review
                and plan.get("visual_review_policy") != "LOCAL_MULTIMODAL_REQUIRED"):
            findings.append({"code": "SCENE.VISUAL_REVIEW_POLICY_MISSING",
                             "severity": "BLOCKING",
                             "detail": "生产候选必须启用本机多模态图片语义复核策略。"})
        expected_language_support = language_support_record(source_pages)
        if plan.get("language_support") != expected_language_support:
            findings.append({"code": "SCENE.LANGUAGE_SUPPORT_IDENTITY_MISMATCH",
                             "severity": "BLOCKING",
                             "detail": "场景计划中的输入语言提示与当前来源页不一致或缺失。"})
        if plan.get("plan_sha256") != _plan_hash(plan):
            findings.append({"code": "SCENE.PLAN_HASH_INVALID", "severity": "BLOCKING",
                             "detail": "冻结场景计划的规范化哈希不匹配。"})
        if plan.get("source_pptx_sha256") != _sha(source_pptx):
            findings.append({"code": "SCENE.SOURCE_IDENTITY_MISMATCH", "severity": "BLOCKING",
                             "detail": "场景计划没有绑定当前原始 PPTX。"})
        if plan.get("policy_sha256") != POLICY_SHA256:
            findings.append({"code": "SCENE.POLICY_IDENTITY_MISMATCH", "severity": "BLOCKING",
                             "detail": "场景计划与当前场景规则版本不一致。"})
        if int(plan.get("source_page_count", -1)) != len(source_pages):
            findings.append({"code": "SCENE.PAGE_COUNT_MISMATCH", "severity": "BLOCKING",
                             "detail": "冻结场景计划的原稿页数与当前输入不一致。"})
        _validate_page_coverage(plan, source_pages, findings)
        scenes = plan.get("scenes") or []
        scene_by_key = {str(scene.get("scene_key") or ""): scene for scene in scenes
                        if isinstance(scene, dict)}
        for scene in scenes:
            try:
                _privacy_query_check(scene, source_pages)
            except SemanticSceneError as exc:
                findings.append({"code": exc.code, "severity": "BLOCKING",
                                 "scene_key": scene.get("scene_key"),
                                 "detail": exc.detail})
        scene_ids = [str(s.get("scene_key") or "") for s in scenes]
        if not scenes:
            findings.append({"code": "SCENE.NO_SCENES_PLANNED", "severity": "BLOCKING",
                             "detail": "硬规则要求每套有实质内容的稿件至少包含一张有关联场景照片。"})
        if len(scene_ids) != len(set(scene_ids)):
            findings.append({"code": "SCENE.SCENE_KEY_DUPLICATED", "severity": "BLOCKING",
                             "detail": "同一具体场景被重复定义。"})
        # The plan may be hand-edited or corrupted after planning. Enforce the
        # same canonical subject/action/setting uniqueness again at the gate,
        # independently of the model-generated scene_key.
        identity_owner = {}
        localized_identity_owner = {}
        for scene in scenes:
            key = str(scene.get("scene_key") or "")
            canonical_scene_id = str(scene.get("canonical_scene_id") or "")
            if not _KEY_RE.fullmatch(canonical_scene_id):
                findings.append({"code": "SCENE.CANONICAL_SCENE_ID_MISSING",
                                 "severity": "BLOCKING", "scene_key": key,
                                 "detail": "每个场景都必须有跨语言稳定的 canonical_scene_id。"})
            identity = _identity_signature(scene)
            if not all(_norm(scene.get(field, ""))
                       for field in ("subject", "action", "setting")):
                findings.append({"code": "SCENE.IDENTITY_INVALID", "severity": "BLOCKING",
                                 "scene_key": key,
                                 "detail": "场景必须有可核验的主体、动作/用途和环境。"})
                continue
            previous = identity_owner.get(identity)
            if previous is not None:
                findings.append({"code": "SCENE.DUPLICATE_SCENE_IDENTITY", "severity": "BLOCKING",
                                 "actual": {"scene_keys": [previous, key],
                                            "identity": identity},
                                 "detail": "同一主体、动作/用途和环境不能用不同场景键重复插图。"})
            else:
                identity_owner[identity] = key
            localized_identity = _localized_identity_signature(scene)
            previous_localized = localized_identity_owner.get(localized_identity)
            if previous_localized is not None and previous_localized != key:
                findings.append({"code": "SCENE.DUPLICATE_SCENE_IDENTITY", "severity": "BLOCKING",
                                 "actual": {"scene_keys": [previous_localized, key],
                                            "identity": localized_identity},
                                 "detail": "相同的主体、动作/用途和环境不能因场景键不同而重复插图。"})
            else:
                localized_identity_owner[localized_identity] = key
        images = plan.get("images") or {}
        # 版位容量不足而未放置的场景如实登记在 unplaced_scenes 里，
        # 它们既没有被文字卡片替代，也没有被悄悄抹掉；其余缺口仍然阻断。
        unplaced = plan.get("unplaced_scenes") or []
        unplaced_keys = {str(item.get("scene_key")) for item in unplaced
                         if isinstance(item, dict) and item.get("scene_key")}
        if any(str((item or {}).get("reason")) not in CAPACITY_UNRESOLVED_REASONS
               for item in unplaced):
            findings.append({"code": "SCENE.UNPLACED_REASON_INVALID", "severity": "BLOCKING",
                             "actual": unplaced,
                             "detail": "只有版位容量类原因可以记为未放置；其余缺口必须阻断。"})
        # 显式开启的文字卡片兜底（CLI --allow-text-card-fallback）会在计划里留下
        # text_cards。它从不冒充照片，也不计入配图数；但既然生成端已经交付了它，
        # 验收端就必须按同一口径结账——否则同一条显式开关产出的候选永远过不了核对
        # （2026-10-26 job-c0af57f36099：run 出了卡片，validate 判 SCENE.UNRESOLVED_SCENES）。
        text_cards = plan.get("text_cards") or {}
        text_card_keys = {str(key) for key in text_cards if str(key).strip()}
        for key in sorted(text_card_keys):
            card = text_cards.get(key) or {}
            if not str(card.get("fallback_reason") or "").strip():
                findings.append({"code": "SCENE.TEXT_CARD_UNDISCLOSED", "severity": "BLOCKING",
                                 "scene_key": key,
                                 "detail": "文字卡片兜底必须如实登记未配图原因，不得冒充已配图。"})
        if text_card_keys & set(images):
            findings.append({"code": "SCENE.TEXT_CARD_OVERLAPS_PHOTO", "severity": "BLOCKING",
                             "scene_keys": sorted(text_card_keys & set(images)),
                             "detail": "同一场景不能既有真实照片又有文字卡片兜底。"})
        if set(images) | unplaced_keys | text_card_keys != set(scene_ids):
            findings.append({"code": "SCENE.UNRESOLVED_SCENES", "severity": "BLOCKING",
                             "expected": scene_ids, "actual": sorted(images),
                             "unplaced": sorted(unplaced_keys),
                             "text_cards": sorted(text_card_keys),
                             "unresolved": plan.get("unresolved_scenes"),
                             "detail": "每个具体场景都必须获得一张合规照片和安全放置位，"
                                       "或如实登记为版位不足未放置／显式文字卡片兜底。"})
        if plan.get("unresolved_scenes"):
            findings.append({"code": "SCENE.UNRESOLVED_SCENES", "severity": "BLOCKING",
                             "actual": plan.get("unresolved_scenes"),
                             "detail": "有场景照片、页面或位置未解决，不能导出纯文字替代稿。"})
        if not images:
            findings.append({"code": "SCENE.NO_SCENE_IMAGES", "severity": "BLOCKING",
                             "detail": "整套候选没有实际计划嵌入的关联场景图。"})
        for key, spec in images.items():
            if key != spec.get("scene_key"):
                findings.append({"code": "SCENE.SCENE_KEY_MISMATCH", "severity": "BLOCKING",
                                 "scene_key": key, "detail": "场景计划映射键与图片条目不一致。"})
            if not _KEY_RE.fullmatch(str(key)):
                findings.append({"code": "SCENE.KEY_INVALID", "severity": "BLOCKING",
                                 "scene_key": key, "detail": "场景键格式无效。"})
            spec_license = spec.get("license", "")
            if spec_license in ("AI_GENERATED_LOCAL", "AI_GENERATED_CLOUD"):
                # See the matching branch in build_plan's per-scene resolution
                # loop: only generate_one() ever sets these literal strings, and
                # neither a local nor a cloud render has an external source page
                # or rightsholder to verify.
                spec_license_ok = bool(spec.get("source_key")) and bool(
                    str(spec.get("creator") or spec.get("attribution") or "").strip())
            else:
                spec_license_ok = (
                    str(spec.get("source_page") or "").startswith("https://")
                    and (str(spec.get("provider") or "").lower() != "pexels"
                         or is_valid_pexels_source_page(spec.get("source_page") or ""))
                    and (str(spec.get("provider") or "").lower() != "pixabay"
                         or is_valid_pixabay_source_page(spec.get("source_page") or ""))
                    and bool(spec.get("source_key"))
                    and not (spec.get("license_url") and not is_valid_license_url(
                        spec.get("license_url"), spec_license))
                    and bool(str(spec.get("creator") or spec.get("attribution") or "").strip())
                    and is_allowed_license(
                        spec_license, version=spec.get("license_version"),
                        license_url=spec.get("license_url")))
            if not spec_license_ok:
                findings.append({"code": "SCENE.LICENSE_METADATA_MISSING", "severity": "BLOCKING",
                                 "scene_key": key,
                                 "detail": "场景照片必须有来源身份、作者/署名和可核验许可；仅接受 CC0、PDM、公有领域、CC BY 4.0、带作者/来源链接的 Pexels License，或本机生图。"})
            query = str(spec.get("visual_query") or "")
            source_scene = scene_by_key.get(str(key), {})
            if (source_scene.get("visual_queries")
                    and query not in source_scene.get("visual_queries", [])):
                findings.append({"code": "SCENE.QUERY_NOT_IN_CANDIDATES", "severity": "BLOCKING",
                                 "scene_key": key,
                                 "detail": "实际检索词不属于该场景经过隐私与格式校验的中性查询候选。"})
            if (source_scene.get("depiction_type", "illustrative") != "illustrative"
                    or spec.get("depiction_type", "illustrative") != "illustrative"
                    or not str(source_scene.get("illustration_rationale") or "").strip()
                    or not str(spec.get("illustration_rationale") or "").strip()):
                findings.append({"code": "SCENE.ILLUSTRATIVE_DISCLOSURE_MISSING",
                                 "severity": "BLOCKING", "scene_key": key,
                                 "detail": "每张配图必须绑定说明性场景理由，且不得暗示图片是原文事件证据。"})
            match_query = (str(source_scene.get("visual_query") or query)
                           if source_scene.get("photo_search_terms") else query)
            match_context = {**source_scene, "metadata_query": match_query}
            # 与 build_plan 的逐场景循环同一规则：AI 生图的提示词不是第三方元数据，
            # 用锚点回验它是循环论证。它的证据是下方按嵌入字节重新绑定的像素复核。
            ai_generated_spec = str(spec_license or "") in ("AI_GENERATED_LOCAL",
                                                            "AI_GENERATED_CLOUD")
            if ai_generated_spec:
                score = float(spec.get("metadata_relevance") or 1.0)
                if not is_safe_metadata(spec):
                    findings.append({"code": "SCENE.CONTENT_SAFETY_METADATA_BLOCK",
                                     "severity": "BLOCKING", "scene_key": key,
                                     "detail": "生成素材的登记信息标记了不适合普通演示文稿的内容。"})
            else:
                score = scene_metadata_relevance(match_query, spec, scene_context=match_context)
                terms = re.findall(r"[a-z0-9]+", match_query.lower())
                content_terms = {x for x in terms if len(x) >= 3 and x not in {
                    "the", "and", "with", "from", "into", "while", "photo", "image",
                    "picture", "photograph", "wide", "landscape", "scene", "at", "in",
                    "on", "of", "a", "an", "to"}}
                required_terms = min(2, len(content_terms))
                matched_terms = int(round(score * len(content_terms)))
                if not is_safe_metadata(spec):
                    findings.append({"code": "SCENE.CONTENT_SAFETY_METADATA_BLOCK", "severity": "BLOCKING",
                                     "scene_key": key,
                                     "detail": "图片来源元数据标记了不适合普通演示文稿的露骨或图像化内容。"})
                if (score < MIN_METADATA_RELEVANCE or matched_terms < required_terms
                        or not spec.get("title") or not query):
                    findings.append({"code": "SCENE.IMAGE_RELEVANCE_UNVERIFIED", "severity": "BLOCKING",
                                     "scene_key": key, "metadata_relevance": round(score, 4),
                                     "required_metadata_relevance": MIN_METADATA_RELEVANCE,
                                     "detail": "照片标题/说明没有足够场景关键词依据，不能确认它对应原文语义。"})
                if not photo_style_matches_metadata(source_scene.get("style"), spec):
                    findings.append({"code": "SCENE.PHOTO_STYLE_MISMATCH", "severity": "BLOCKING",
                                     "scene_key": key,
                                     "detail": "嵌入素材的元数据表明它是插画/绘画，不符合该场景要求的摄影风格。"})
                if not scene_metadata_matches_query(match_query, spec,
                                                    scene_context=match_context):
                    findings.append({"code": "SCENE.IMAGE_CONTEXT_UNVERIFIED", "severity": "BLOCKING",
                                     "scene_key": key,
                                     "detail": "嵌入图片的标题/说明没有同时支持场景主体、动作和地点锚点。"})
            declared_score = spec.get("metadata_relevance")
            if (not ai_generated_spec and declared_score is not None
                    and abs(float(declared_score) - score) > 0.0001):
                findings.append({"code": "SCENE.IMAGE_RELEVANCE_IDENTITY_MISMATCH", "severity": "BLOCKING",
                                 "scene_key": key,
                                 "detail": "冻结计划中的场景图相关性分数与来源元数据不一致。"})
            try:
                page = int(spec.get("page"))
                rect = [float(x) for x in spec.get("rect", [])]
            except (TypeError, ValueError):
                page, rect = -1, []
            if page < 1 or page > len(source_pages) or len(rect) != 4:
                findings.append({"code": "SCENE.PLACEMENT_INVALID", "severity": "BLOCKING",
                                 "scene_key": key, "detail": "场景图页码或矩形无效。"})
            if float(spec.get("area_ratio") or 0) < _MIN_AREA_RATIO:
                findings.append({"code": "SCENE.PICTURE_TOO_SMALL", "severity": "BLOCKING",
                                 "scene_key": key, "actual": spec.get("area_ratio"),
                                 "expected": _MIN_AREA_RATIO,
                                 "detail": "场景照片面积不足以形成有效视觉解释。"})
            fit_mode = str(spec.get("fit_mode") or "")
            if fit_mode not in {"contain", "cover"}:
                findings.append({"code": "SCENE.PHOTO_FIT_MODE_INVALID", "severity": "BLOCKING",
                                 "scene_key": key,
                                 "detail": "照片必须声明可复核的完整容纳或受控裁切模式。"})
            if spec.get("placement_mode") == "empty_area" and fit_mode != "contain":
                findings.append({"code": "SCENE.PHOTO_UNPLANNED_CROP", "severity": "BLOCKING",
                                 "scene_key": key,
                                 "detail": "空白版位必须完整显示场景照片，不允许为塞入横条而裁掉主体。"})
            if fit_mode == "cover":
                if spec.get("placement_mode") != "replace_visual_image":
                    findings.append({"code": "SCENE.PHOTO_COVER_SLOT_INVALID", "severity": "BLOCKING",
                                     "scene_key": key,
                                     "detail": "cover 裁切只允许用于经规划的现有视觉图片槽。"})
                try:
                    image_aspect = float(spec.get("actual_width") or 0) / max(
                        1.0, float(spec.get("actual_height") or 0))
                    target_rect = [float(x) for x in spec.get("target_rect", spec.get("rect", []))]
                    box_aspect = target_rect[2] / target_rect[3] if len(target_rect) == 4 else 0.0
                except (TypeError, ValueError, ZeroDivisionError, IndexError):
                    image_aspect, box_aspect = 0.0, 0.0
                factor = _image_to_box_aspect_factor(image_aspect, box_aspect)
                if factor is None or factor > max_replacement_aspect_factor + 0.0001:
                    findings.append({"code": "SCENE.PHOTO_CROP_EXCESSIVE", "severity": "BLOCKING",
                                     "scene_key": key,
                                     "actual_aspect_factor": factor,
                                     "maximum_aspect_factor": max_replacement_aspect_factor,
                                     "detail": "替换视觉图片槽的源图与版位比例差超过 HG13 上限。"})
            try:
                actual_w = int(spec.get("actual_width") or 0)
                actual_h = int(spec.get("actual_height") or 0)
            except (TypeError, ValueError):
                actual_w, actual_h = 0, 0
            if (actual_w > 0 and actual_w < MIN_IMAGE_WIDTH) or (actual_h > 0 and actual_h < MIN_IMAGE_HEIGHT):
                findings.append({"code": "SCENE.IMAGE_RESOLUTION_TOO_LOW", "severity": "BLOCKING",
                                 "scene_key": key, "actual": (actual_w, actual_h),
                                 "expected_minimum": (MIN_IMAGE_WIDTH, MIN_IMAGE_HEIGHT),
                                 "detail": "场景照片分辨率过低，无法保证清晰展示。"})
        markers = _marked_shapes(candidate_pptx)
        embedded_image_blobs = {}
        if require_visual_review:
            try:
                _, embedded_review_inputs = _scene_picture_pixel_inputs(candidate_pptx)
                embedded_image_blobs = {
                    key: rows[0]["image_blob"]
                    for key, rows in embedded_review_inputs.items()
                    if len(rows) == 1 and rows[0].get("image_blob")
                }
            except Exception:
                # The visual proof below will fail closed for every scene whose
                # actual embedded bytes cannot be re-read and rebound.
                embedded_image_blobs = {}
        malformed = [m for m in markers if m.get("malformed")]
        if malformed:
            findings.append({"code": "SCENE.MARKER_OR_IMAGE_INVALID", "severity": "BLOCKING",
                             "actual": len(malformed),
                             "detail": "场景图片标记或嵌入图片字节不可核验。"})
        counts = Counter(m.get("scene_key") for m in markers if m.get("scene_key"))
        # 恰有一张真实嵌入图的对象 = 计划里真的配了图的场景。文字卡片兜底与
        # 版位不足未放置的场景不带任何图片标记，也不得被算作已配图。
        expected_picture_keys = set(images)
        if set(counts) != expected_picture_keys or any(count != 1
                                                       for count in counts.values()):
            findings.append({"code": "SCENE.PICTURE_COVERAGE_MISMATCH", "severity": "BLOCKING",
                             "expected": sorted(expected_picture_keys), "actual": dict(counts),
                             "text_cards": sorted(text_card_keys),
                             "unplaced": sorted(unplaced_keys),
                             "detail": "每个已配图场景必须在全稿中恰有一张真实嵌入图片；"
                                       "文字卡片与未放置场景不得携带图片。"})
        by_key = {m.get("scene_key"): m for m in markers if m.get("scene_key")}
        seen_sha, seen_source, seen_phashes = {}, {}, []
        for key, spec in images.items():
            marker = by_key.get(key)
            if not marker:
                continue
            expected_sha = str(spec.get("sha256") or "")
            if marker["actual_sha256"] != expected_sha or marker["marker_sha256"] != expected_sha:
                findings.append({"code": "SCENE.PICTURE_IDENTITY_MISMATCH", "severity": "BLOCKING",
                                 "scene_key": key, "expected_sha256": expected_sha,
                                 "actual_sha256": marker["actual_sha256"],
                                 "detail": "验收从 PPTX picture.image.blob 重算哈希，不信任对象标记自报值。"})
            if require_visual_review:
                review = spec.get("visual_review")
                scene = scene_by_key.get(str(key), {})
                response_sha = (str(review.get("response_sha256") or "")
                                if isinstance(review, dict) else "")
                try:
                    expected_prompt_sha = hashlib.sha256(
                        scene_image_review_prompt(scene).encode("utf-8")
                    ).hexdigest()
                    expected_result_sha = scene_review_result_sha256(
                        review if isinstance(review, dict) else {})
                    expected_input_sha = scene_review_payload_sha256(
                        embedded_image_blobs.get(str(key), b""))
                except Exception:
                    expected_prompt_sha = expected_result_sha = expected_input_sha = ""
                review_valid = (
                    isinstance(review, dict)
                    and review.get("schema_version") == "WG-LOCAL-SCENE-IMAGE-REVIEW/1"
                    and review.get("status") == "PASSED"
                    and review.get("image_sha256") == marker["actual_sha256"]
                    and review.get("visual_input_sha256") == expected_input_sha
                    and review.get("scene_identity_sha256") ==
                        scene_visual_identity_sha256(scene)
                    and review.get("prompt_sha256") == expected_prompt_sha
                    and review.get("review_result_sha256") == expected_result_sha
                    and re.fullmatch(r"[0-9a-f]{64}", response_sha) is not None
                    and bool(str(review.get("model") or "").strip())
                    and all(review.get(field) is True for field in (
                        "scene_match", "subject_visible", "action_visible",
                        "setting_visible", "photographic"))
                    and review.get("confidence") == "high"
                    and review.get("reason_code") == "NONE")
                if not review_valid:
                    findings.append({"code": "SCENE.VISUAL_REVIEW_UNVERIFIED",
                                     "severity": "BLOCKING", "scene_key": key,
                                     "detail": "缺少绑定实际嵌入图片字节与具体场景的高置信度本机视觉复核。"})
            if spec.get("perceptual_hash") != marker.get("actual_perceptual_hash"):
                findings.append({"code": "SCENE.PERCEPTUAL_HASH_MISMATCH", "severity": "BLOCKING",
                                 "scene_key": key, "expected": spec.get("perceptual_hash"),
                                 "actual": marker.get("actual_perceptual_hash"),
                                 "detail": "图片感知哈希必须与实际嵌入字节一致。"})
            if marker["page"] != int(spec.get("page", -1)):
                findings.append({"code": "SCENE.PICTURE_PAGE_MISMATCH", "severity": "BLOCKING",
                                 "scene_key": key, "expected": spec.get("page"),
                                 "actual": marker["page"], "detail": "场景照片放在错误页面。"})
            if not _same_rect(marker["rect"], spec.get("rect", []), tol=0.006):
                findings.append({"code": "SCENE.PICTURE_RECT_MISMATCH", "severity": "BLOCKING",
                                 "scene_key": key, "expected": spec.get("rect"),
                                 "actual": marker["rect"], "detail": "场景照片没有按冻结安全区放置。"})
            if marker["area_ratio"] < _MIN_AREA_RATIO:
                findings.append({"code": "SCENE.PICTURE_TOO_SMALL", "severity": "BLOCKING",
                                 "scene_key": key, "actual": marker["area_ratio"],
                                 "expected": _MIN_AREA_RATIO,
                                 "detail": "实际嵌入图片面积太小。"})
            if spec.get("fit_mode") == "contain":
                image_aspect = float(spec.get("actual_width") or 0) / max(
                    1.0, float(spec.get("actual_height") or 0))
                relative_error = abs(marker["aspect_ratio"] - image_aspect) / max(0.001, image_aspect)
                if relative_error > 0.02:
                    findings.append({"code": "SCENE.PHOTO_ASPECT_MISMATCH", "severity": "BLOCKING",
                                     "scene_key": key, "actual": marker["aspect_ratio"],
                                     "expected": image_aspect,
                                     "detail": "场景照片的显示比例与原始画面不符，可能挤压或裁切了主体。"})
                if any(abs(value) > 0.0001 for value in marker.get("crop", [])):
                    findings.append({"code": "SCENE.PHOTO_UNEXPECTED_CROP", "severity": "BLOCKING",
                                     "scene_key": key, "actual": marker.get("crop"),
                                     "detail": "计划完整显示的场景照片仍存在裁切参数。"})
            elif spec.get("fit_mode") == "cover":
                image_aspect = float(spec.get("actual_width") or 0) / max(
                    1.0, float(spec.get("actual_height") or 0))
                cover_rect = spec.get("target_rect", spec.get("rect", []))
                try:
                    box_aspect = float(cover_rect[2]) / max(0.001, float(cover_rect[3]))
                except (TypeError, ValueError, IndexError):
                    box_aspect = 0.0
                if not _cover_crop_matches_geometry(image_aspect, box_aspect,
                                                    marker.get("crop", [])):
                    findings.append({"code": "SCENE.PHOTO_CROP_GEOMETRY_MISMATCH",
                                     "severity": "BLOCKING", "scene_key": key,
                                     "actual_crop": marker.get("crop"),
                                     "image_aspect": image_aspect, "box_aspect": box_aspect,
                                     "detail": "实际嵌入图片的 OOXML 裁切不符合冻结源图与版位比例。"})
            source_key = str(spec.get("source_key") or "")
            if expected_sha in seen_sha:
                findings.append({"code": "SCENE.REUSED_PHOTO", "severity": "BLOCKING",
                                 "actual": {"sha256": expected_sha,
                                            "scene_keys": [seen_sha[expected_sha], key]},
                                 "detail": "同一张场景照片在全稿中只能出现一次。"})
            seen_sha[expected_sha] = key
            current_phash = str(marker.get("actual_perceptual_hash") or "")
            for previous_key, previous_phash in seen_phashes:
                if image_hash_distance(current_phash, previous_phash) <= 4:
                    findings.append({"code": "SCENE.NEAR_DUPLICATE_PHOTO", "severity": "BLOCKING",
                                     "actual": {"scene_keys": [previous_key, key],
                                                "perceptual_hashes": [previous_phash, current_phash]},
                                     "detail": "全稿不得重复或近重复使用同一张场景照片。"})
                    break
            seen_phashes.append((key, current_phash))
            if source_key in seen_source and source_key:
                findings.append({"code": "SCENE.REUSED_SOURCE_PHOTO", "severity": "BLOCKING",
                                 "actual": {"source_key": source_key,
                                            "scene_keys": [seen_source[source_key], key]},
                                 "detail": "同一来源照片不能以不同文件形式重复使用。"})
            if source_key:
                seen_source[source_key] = key
            if (not marker["alt_text"] or str(spec.get("license")) not in marker["alt_text"]
                    or str(spec.get("source_page")) not in marker["alt_text"]
                    or "Depiction: Illustrative scene; not evidence of a specific person, event, company, or outcome."
                    not in marker["alt_text"]):
                findings.append({"code": "SCENE.ATTRIBUTION_MISSING", "severity": "BLOCKING",
                                 "scene_key": key,
                                 "detail": "嵌入图片的替代文字没有保留来源与许可信息。"})
            expected_credit = _photo_credit_block(spec, marker.get("crop") or [])
            credit_slide = candidate.slides[int(spec["page"]) - 1]
            notes_text = (credit_slide.notes_slide.notes_text_frame.text
                          if credit_slide.has_notes_slide else "")
            if expected_credit not in notes_text:
                findings.append({"code": "SCENE.ATTRIBUTION_NOTES_MISSING",
                                 "severity": "BLOCKING", "scene_key": key,
                                 "detail": "候选备注没有保留与当前嵌入图身份一致的署名、来源、许可链接及修改说明。"})
            overlap, blockers = _candidate_blockers(candidate_pptx, spec)
            if overlap > _MAX_TEXT_OVERLAP or blockers:
                findings.append({"code": "SCENE.PICTURE_OBSCURED_OR_BLOCKED", "severity": "BLOCKING",
                                 "scene_key": key, "actual": {"text_overlap_ratio": round(overlap, 5),
                                 "blockers": blockers},
                                 "detail": "场景图片与文本卡片/图表/其他图片冲突或被遮挡。"})
        result["summary"].update({
            "planned_scene_count": len(scenes),
            "embedded_scene_picture_count": len(markers),
            "visual_review_required": bool(require_visual_review),
            "visual_review_passed_count": (sum(
                1 for spec in images.values()
                if isinstance(spec.get("visual_review"), dict)
                and spec["visual_review"].get("status") == "PASSED")
                if require_visual_review else 0),
            "unique_embedded_photo_sha256_count": len({m.get("actual_sha256") for m in markers}),
            "all_source_pages_accounted": set(int(r.get("page")) for r in plan.get("page_coverage", [])
                                               if isinstance(r, dict) and str(r.get("page", "")).isdigit())
                                              == set(range(1, len(source_pages) + 1)),
            "rendered_pixel_visibility": "not_evaluated_by_HG13_requires_separate_rendered_layout_review",
            "findings": len(findings),
        })
    except Exception as exc:
        if not any(f.get("code") == "SCENE.CHECK_NOT_EVALUABLE" for f in findings):
            findings.append({"code": "SCENE.CHECK_NOT_EVALUABLE", "severity": "BLOCKING",
                             "detail": f"HG13 缺少可复核证据：{type(exc).__name__}: {exc}"})
    result["status"] = "FAILED" if findings else "EXECUTED"
    result["summary"]["policy_sha256"] = _sha(POLICY_FILE) if POLICY_FILE.exists() else None
    return result


def _photo_changes_text(crop) -> str:
    try:
        cropped = any(abs(float(value)) > 1e-6 for value in crop)
    except (TypeError, ValueError):
        cropped = True
    if cropped:
        return "Displayed with crop-to-fit in slide frame; original downloaded bytes unchanged."
    return "Embedded without crop or raster edits; scaled and positioned in slide frame."


def _photo_credit_block(spec: dict, crop) -> str:
    title = re.sub(r"\s+", " ", str(spec.get("title") or spec.get("label") or "Scene photo")).strip()
    creator = re.sub(r"\s+", " ", str(spec.get("attribution") or spec.get("creator") or "")).strip()
    source = re.sub(r"\s+", " ", str(spec.get("source_page") or "")).strip()
    license_name = re.sub(r"\s+", " ", str(spec.get("license") or "")).strip()
    license_url = re.sub(r"\s+", " ", str(spec.get("license_url") or "")).strip()
    illustration_rationale = re.sub(r"\s+", " ",
                                    str(spec.get("illustration_rationale") or "")).strip()
    license_part = f"{license_name} ({license_url})" if license_url else license_name
    return "\n".join((
        "[WENGAO PHOTO CREDIT]",
        "Depiction: Illustrative scene; not evidence of a specific person, event, company, or outcome.",
        f"Illustration basis: {illustration_rationale}",
        f"Title: {title}",
        f"Creator / attribution: {creator}",
        f"Source: {source}",
        f"License: {license_part}",
        f"Changes: {_photo_changes_text(crop)}",
    ))


def _slide_background_rgb(slide) -> tuple[int, int, int]:
    try:
        fill = slide.background.fill
        if fill.type is not None and fill.fore_color.type is not None:
            hexstr = str(fill.fore_color.rgb)
            return (int(hexstr[0:2], 16), int(hexstr[2:4], 16), int(hexstr[4:6], 16))
    except Exception:
        pass
    return (245, 247, 251)


def _apply_reflow_shapes(slide, reflow_shapes, *, scene_key: str,
                         shapes_by_index: list | None = None) -> None:
    """Shared by photo and text-card placement: move/resize whatever text the
    placement decision already determined must yield, by the same frozen
    geometry either one computes. Never touches anything not explicitly listed.

    `shapes_by_index` must be the shape order the plan itself saw — pass it
    in (shared across every scene on this page) rather than defaulting to a
    fresh `list(slide.shapes)` here: once an earlier scene on the same page
    has inserted a photo and reordered the slide's XML, a fresh list no
    longer matches the index the plan computed its `shape_index` against.
    """
    if shapes_by_index is None:
        shapes_by_index = list(slide.shapes)
    for reflow in reflow_shapes or []:
        target = None
        idx = reflow.get("shape_index")
        if isinstance(idx, int) and 0 <= idx < len(shapes_by_index):
            target = shapes_by_index[idx]
        else:
            name = reflow.get("name")
            if name:
                matches = [sh for sh in shapes_by_index if sh.name == name]
                if len(matches) == 1:
                    target = matches[0]
        if target is None:
            raise SemanticSceneError(
                "SCENE.REFLOW_TARGET_UNRESOLVED",
                f"场景 {scene_key} 让位重排的目标形状无法唯一定位；放置判定依赖"
                "该重排，不得在未让位的文字上压图/压卡片。")
        new_w = reflow.get("width")
        new_left = reflow.get("left")
        new_t = reflow.get("top")
        new_h = reflow.get("height")
        # python-pptx placeholders with no own <a:xfrm> inherit left/top/
        # width/height from the layout; setting only SOME of the four adds a
        # partial xfrm that silently zeros the rest (reproduced and root-
        # caused this session — see Sh.move()/T.move_to()). Write all four
        # together every time, falling back to the shape's own current
        # (possibly inherited) value for whichever one this reflow didn't
        # specify, so a one-axis resize can never zero the other axis.
        if new_w is not None or new_left is not None or new_t is not None or new_h is not None:
            target.width = Inches(float(new_w)) if new_w is not None else target.width
            target.left = Inches(float(new_left)) if new_left is not None else target.left
            target.top = Inches(float(new_t)) if new_t is not None else target.top
            target.height = Inches(float(new_h)) if new_h is not None else target.height
        if reflow.get("line_spacing") is not None:
            for paragraph in target.text_frame.paragraphs:
                paragraph.line_spacing = float(reflow["line_spacing"])
                paragraph.space_after = Pt(float(reflow.get("space_after_pt", 4.0)))


def _draw_text_card(slide, spec: dict, *, shapes_by_index: list | None = None) -> dict:
    """Draw a plainly-labeled placeholder where a verified, licensed photo
    could not be found for this scene — explicit opt-in fallback only (see
    allow_text_card_fallback on build_plan). Never pretends to be a photo: a
    muted panel in the deck's own background tone, showing the scene's own
    subject/action/setting and an explicit disclosure line, so a reader can
    tell at a glance this is a fallback, not real content."""
    _apply_reflow_shapes(slide, spec.get("reflow_shapes"), scene_key=spec["scene_key"],
                         shapes_by_index=shapes_by_index)
    left, top, width, height = [float(x) for x in spec["rect"]]
    bg_rgb = _slide_background_rgb(slide)
    luminance = 0.299 * bg_rgb[0] + 0.587 * bg_rgb[1] + 0.114 * bg_rgb[2]
    if luminance < 128:
        card_rgb = tuple(min(255, c + 24) for c in bg_rgb)
        text_rgb, caption_rgb = (235, 238, 244), (158, 170, 190)
    else:
        card_rgb = tuple(max(0, c - 18) for c in bg_rgb)
        # caption_rgb (110, 120, 135) measured 3.82:1 against the common white-
        # background card tone (237, 237, 237) — under HG09's 4.5:1 floor,
        # blocking every text-card fallback on a light theme (2026-10-31
        # job-ed0b23aa8f45, 102 of 157 layout_hard_gate findings, all
        # HG09/LOCAL_CONTRAST on this shape). A first fix darkened it to
        # (98, 107, 120) — 4.61:1 in exact-RGB math — but HG09 samples the
        # *rendered* glyph pixels, and small-text anti-aliasing blends toward
        # the background by up to ~14/channel in practice (job-aa7163f0d0f7:
        # measured samples up to (112, 120, 132), only 3.81:1). Darkened
        # further to withstand that: 6.05:1 nominal, still ~4.7:1 even under a
        # conservative +16/channel rendering-softening estimate.
        text_rgb, caption_rgb = (30, 38, 50), (81, 89, 100)
    box = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE,
                                 Inches(left), Inches(top), Inches(width), Inches(height))
    box.fill.solid()
    box.fill.fore_color.rgb = RGBColor(*card_rgb)
    box.line.fill.background()
    box.shadow.inherit = False
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = Inches(0.3)
    tf.margin_top = tf.margin_bottom = Inches(0.25)
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    label = str(spec.get("label") or spec.get("subject") or "场景说明").strip()
    caption = " · ".join(p for p in
                         (spec.get("subject"), spec.get("action"), spec.get("setting")) if p)
    # The layout gate classifies this whole card as role="body" (13-18pt) —
    # the label used to be hardcoded Pt(20) (over the ceiling) and the
    # disclosure line Pt(10) (under the floor), both unconditionally
    # HG07/ROLE_SIZE findings (job-aa7163f0d0f7, pages 6/7/9/11). Derive all
    # three sizes from the same ROLE_RANGE_PT["body"] range the gate checks
    # against, so the card stays internally consistent with itself.
    body_lo, body_hi = ROLE_RANGE_PT["body"]
    label_pt, secondary_pt = Pt(body_hi), Pt(body_lo)
    tf.text = label
    p0 = tf.paragraphs[0]
    p0.alignment = PP_ALIGN.CENTER
    for r in p0.runs:
        r.font.size, r.font.bold = label_pt, True
        r.font.color.rgb = RGBColor(*text_rgb)
    if caption:
        p1 = tf.add_paragraph()
        p1.text = caption
        p1.alignment = PP_ALIGN.CENTER
        p1.space_before = Pt(6)
        for r in p1.runs:
            r.font.size = secondary_pt
            r.font.color.rgb = RGBColor(*caption_rgb)
    p2 = tf.add_paragraph()
    p2.text = "未找到可核验的合规配图，以说明卡片代替"
    p2.alignment = PP_ALIGN.CENTER
    p2.space_before = Pt(10)
    for r in p2.runs:
        r.font.size, r.font.italic = secondary_pt, True
        r.font.color.rgb = RGBColor(*caption_rgb)
    visual_mod.insert_below_text(slide, box._element)
    return {"shape_id": box.shape_id, "shape_name": box.name}


def apply_plan(pptx_path: str | Path, plan: dict, output_path: str | Path, *, allow_fallback: bool = False) -> dict:
    """Embed each licensed scene photo once, replacing a safe generic image slot when present."""
    src, out = Path(pptx_path), Path(output_path)
    if not plan.get("unresolved_scenes") and not plan.get("images") \
            and not plan.get("text_cards") and plan.get("unplaced_scenes"):
        # 全部场景都因版位不足未放置：没有图要嵌，原样写出即可。
        prs = Presentation(str(src))
        prs.save(str(out))
        return {"schema": "rs.scene_actions.v1", "actions": [], "inserted": 0,
                "status": "NO_SCENE_CAPACITY",
                "unplaced_scenes": plan.get("unplaced_scenes")}
    if plan.get("unresolved_scenes") or (not plan.get("images") and not plan.get("text_cards")):
        if allow_fallback:
            prs = Presentation(str(src))
            prs.save(str(out))
            return {
                "schema": "rs.scene_actions.v1",
                "actions": [],
                "status": "FALLBACK_T4",
                "unresolved_scenes": plan.get("unresolved_scenes", []),
            }
        raise SemanticSceneError("SCENE.UNRESOLVED_SCENES",
                                 "存在未解决场景或整稿没有场景图；拒绝生成无场景候选。")
    if plan.get("layout_input_sha256") != _sha(src):
        raise SemanticSceneError("SCENE.LAYOUT_IDENTITY_MISMATCH",
                                 "放置计划对应的版式文件已变化，需重新规划。")
    prs = Presentation(str(src))
    actions = []
    # `reflow_shapes[*].shape_index` is only a stable identity against the
    # shape order *as the plan saw it* — a fresh list, not `list(slide.shapes)`.
    # Taking that fresh each scene breaks on the very first multi-scene page
    # it's for: `insert_below_text` (below) reorders the slide's XML so the
    # picture sits before the text shapes, so by the time scene N+1 on the
    # same page recomputes the list, every later shape has shifted by one
    # and scene N+1's reflow lands on a different shape than the plan meant
    # (2026-10-27: this misrouted a title reflow onto the title box, pushing
    # the body text down into a photo it was reflowed to make room for).
    # Caching the pre-mutation list per page and reusing it for every scene
    # on that page keeps the index meaning what the plan computed it to mean.
    shapes_by_index_cache: dict[int, list] = {}

    def _shapes_by_index(page_no: int) -> list:
        if page_no not in shapes_by_index_cache:
            shapes_by_index_cache[page_no] = list(prs.slides[page_no - 1].shapes)
        return shapes_by_index_cache[page_no]

    for scene_key, spec in sorted(plan["images"].items(), key=lambda row: (row[1]["page"], row[0])):
        page = int(spec["page"])
        slide = prs.slides[page - 1]
        image_path = Path(spec["file"])
        if not image_path.is_file() or _sha(image_path) != spec["sha256"]:
            raise SemanticSceneError("SCENE.PHOTO_IDENTITY_CHANGED",
                                     f"场景 {scene_key} 的本地照片在插入前身份变化。")
        left, top, width, height = [float(x) for x in spec["rect"]]
        old_picture = None
        if spec.get("placement_mode") == "replace_visual_image":
            target_r = spec.get("target_rect", spec["rect"])
            matches = [shape for shape in slide.shapes
                       if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE
                       and (_same_rect(_shape_rect(shape), target_r) or _same_rect(_shape_rect(shape), spec["rect"]))
                       and _picture_sha(shape) == spec.get("replaced_visual_sha256")]
            if len(matches) != 1:
                matches_by_sha = [shape for shape in slide.shapes
                                  if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE
                                  and _picture_sha(shape) == spec.get("replaced_visual_sha256")]
                if len(matches_by_sha) == 1:
                    matches = matches_by_sha
                else:
                    raise SemanticSceneError("SCENE.VISUAL_SLOT_STALE",
                                             f"第 {page} 页原有通用图片槽与冻结计划不匹配。")
            old_picture = matches[0]
        # 让位重排按**页内序号**定位：很多真实原稿的形状名是空字符串，
        # 旧实现用 `sh.name == name` 匹配，空名会命中该页**每一个**形状，
        # 于是标题/页脚被一起挪走，而真正该让位的文字没让开，配图直接压在
        # 文字上（HG03 字形碰撞）。计划与应用面向同一个版式文件，且上方已校验
        # layout_input_sha256，序号在这一步是稳定身份——但仅当它取自规划时的
        # 形状顺序；本页后续场景必须复用同一份缓存（见上方 _shapes_by_index），
        # 不能在本场景插图改变 XML 顺序之后，为下一个场景重新 list(slide.shapes)。
        shapes_by_index = _shapes_by_index(page)
        for reflow in spec.get("reflow_shapes") or []:
            target = None
            idx = reflow.get("shape_index")
            if isinstance(idx, int) and 0 <= idx < len(shapes_by_index):
                target = shapes_by_index[idx]
            else:
                name = reflow.get("name")
                if name:                      # 空名不是身份，宁可不动也不乱改
                    matches = [sh for sh in shapes_by_index if sh.name == name]
                    if len(matches) == 1:
                        target = matches[0]
            if target is None:
                raise SemanticSceneError(
                    "SCENE.REFLOW_TARGET_UNRESOLVED",
                    "让位重排的目标形状无法唯一定位；放置判定依赖该重排，"
                    "不得在未让位的文字上压图。")
            new_w = reflow.get("width")
            new_left = reflow.get("left")
            new_t = reflow.get("top")
            new_h = reflow.get("height")
            if new_w is not None:
                target.width = Inches(float(new_w))
            if new_left is not None:
                target.left = Inches(float(new_left))
            if new_t is not None:
                target.top = Inches(float(new_t))
            if new_h is not None:
                target.height = Inches(float(new_h))
            if reflow.get("line_spacing") is not None:
                for paragraph in target.text_frame.paragraphs:
                    paragraph.line_spacing = float(reflow["line_spacing"])
                    paragraph.space_after = Pt(float(reflow.get("space_after_pt", 4.0)))
        try:
            pic = slide.shapes.add_picture(str(image_path), Inches(left), Inches(top),
                                           Inches(width), Inches(height))
        except Exception as exc:
            raise SemanticSceneError(
                "SCENE.OBJECT_INSERTION_FAILED",
                f"场景 {scene_key} 图片插入幻灯片对象失败: {exc}",
            ) from exc
        if pic is None or not hasattr(pic, "shape_id"):
            raise SemanticSceneError(
                "SCENE.OBJECT_INSERTION_FAILED",
                f"场景 {scene_key} 图片未能生成有效的幻灯片图形对象。",
            )
        marker = f"WENGAO_SCENE|{scene_key}|{spec['sha256']}"
        # 标记名里带着图片字节的 SHA-256，验收要按它逐字核对。旧实现把整个名字截到
        # 120 字符，场景键一长（≥42 字符）就会切掉哈希尾部，于是验收永远对不上：
        # 2026-10-26 job-c0af57f36099 第 13 页 customer_support_specialist_ticket_response
        # （43 字符）因此被判 SCENE.PICTURE_IDENTITY_MISMATCH，整稿在"核对"环节失败。
        # OOXML 的 cNvPr/@name 没有 120 字符限制；宁可显式报错，也不能静默截断身份。
        if len(marker) > _MAX_SCENE_MARKER_LEN:
            raise SemanticSceneError(
                "SCENE.MARKER_TOO_LONG",
                f"场景 {scene_key} 的图片标记超过 {_MAX_SCENE_MARKER_LEN} 字符；"
                "标记必须完整包含图片哈希，不能截断。")
        c_nv = pic._element.nvPicPr.cNvPr
        c_nv.set("name", marker)
        shape_id = int(pic.shape_id)
        shape_name = str(pic.name)
        if spec.get("fit_mode", "cover") == "cover":
            visual_mod._apply_srcrect(pic, visual_mod._cover_srcrect(
                image_path, width, height, *spec.get("focus", [0.5, 0.5])))
        photo_crop = [float(pic.crop_left), float(pic.crop_top),
                      float(pic.crop_right), float(pic.crop_bottom)]
        credit_block = _photo_credit_block(spec, photo_crop)
        c_nv.set("descr", " | ".join(credit_block.splitlines())[:1024])
        notes_frame = slide.notes_slide.notes_text_frame
        existing_notes = notes_frame.text.rstrip()
        if credit_block not in existing_notes:
            notes_frame.text = (existing_notes + "\n\n" + credit_block).strip()
        if old_picture is not None:
            old_el = old_picture._element
            new_el = pic._element
            old_parent = old_el.getparent()
            old_parent.insert(old_parent.index(old_el), new_el)
            rid_attr = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
            blip = old_el.find("{http://schemas.openxmlformats.org/presentationml/2006/main}blipFill")
            old_rid = None
            if blip is not None and blip.find("{http://schemas.openxmlformats.org/drawingml/2006/main}blip") is not None:
                old_rid = blip.find("{http://schemas.openxmlformats.org/drawingml/2006/main}blip").get(rid_attr)
            old_parent.remove(old_el)
            if old_rid:
                referenced = any(el.get(rid_attr) == old_rid for el in old_parent.iter())
                if not referenced:
                    try:
                        slide.part.drop_rel(old_rid)
                    except Exception:
                        pass
            # Existing visual masks share the same photo slot; lighten them so the new scene remains visible.
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False) and (shape.text_frame.text or "").strip():
                    continue
                if _same_rect(_shape_rect(shape), spec["rect"]) \
                        and getattr(shape, "shape_type", None) != MSO_SHAPE_TYPE.PICTURE \
                        and _has_opaque_fill(shape):
                    visual_mod._set_alpha(shape, 78)
        else:
            visual_mod.insert_below_text(slide, pic._element)
        actions.append({"scene_key": scene_key, "page": page, "sha256": spec["sha256"],
                        "content_hash": spec.get("content_hash") or spec["sha256"],
                        "query": spec.get("visual_query") or spec.get("query") or "",
                        "visual_query": spec.get("visual_query") or spec.get("query") or "",
                        "source": spec.get("source_page") or spec.get("provider") or "",
                        "source_page": spec.get("source_page") or "",
                        "provider": spec.get("provider") or "",
                        "license": spec["license"],
                        "license_url": spec.get("license_url") or "",
                        "download_url": spec.get("download_url") or "",
                        "asset_id": spec.get("asset_id") or spec.get("source_key") or "",
                        "source_key": spec["source_key"],
                        "shape_id": shape_id,
                        "shape_name": shape_name,
                        "object_id": shape_id,
                        "object_name": shape_name,
                        "rect": spec["rect"], "placement_mode": spec["placement_mode"],
                        "area_ratio": spec["area_ratio"],
                        "text_overlap_ratio": spec["text_overlap_ratio"],
                        "crop": photo_crop})

    text_card_actions = []
    for scene_key, spec in sorted((plan.get("text_cards") or {}).items(),
                                  key=lambda row: (row[1]["page"], row[0])):
        page = int(spec["page"])
        slide = prs.slides[page - 1]
        shape_info = _draw_text_card(slide, spec, shapes_by_index=_shapes_by_index(page))
        text_card_actions.append({
            "scene_key": scene_key, "page": page, "rect": spec["rect"],
            "placement_mode": spec["placement_mode"],
            "fallback_reason": spec.get("fallback_reason"),
            "fallback_detail": spec.get("fallback_detail"),
            "shape_id": shape_info["shape_id"], "shape_name": shape_info["shape_name"],
        })
    # Reflowing a text shape to make room for a photo or text-card (above)
    # moves/resizes it to whatever geometry the placement decision computed
    # for the *image*'s sake — it was never checked against how much room
    # the text shape's own real (wrapped) content needs. theme.py's own
    # overlap/overflow correction runs earlier, during styling, before any
    # scene is placed, so it never sees geometry a later reflow introduces.
    # Re-run the same correction here, now that every reflow for this
    # candidate has actually happened, scoped to the pages touched.
    touched_pages = {int(spec["page"]) for spec in plan.get("images", {}).values()}
    touched_pages |= {int(spec["page"]) for spec in (plan.get("text_cards") or {}).values()}
    if touched_pages:
        slide_h_in = prs.slide_height / 914400.0
        slide_w_in = prs.slide_width / 914400.0
        for page in sorted(touched_pages):
            visual_mod.fix_stacked_text_overlap(
                prs.slides[page - 1], acts=None, page=page,
                slide_h_in=slide_h_in, slide_w_in=slide_w_in)
    out.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(out))
    return {"inserted": len(actions), "actions": actions,
           "text_cards_inserted": len(text_card_actions),
           "text_card_actions": text_card_actions,
           "output_sha256": _sha(out)}


def reconcile_visual_plan(visual_plan: dict, scene_plan: dict) -> dict:
    """Keep generic visual-plan identity aligned when a semantic photo replaces its image."""
    if not isinstance(visual_plan, dict):
        return visual_plan
    for scene_key, spec in (scene_plan.get("images") or {}).items():
        if spec.get("placement_mode") != "replace_visual_image":
            continue
        page_key = str(spec["page"])
        current = (visual_plan.get("images") or {}).get(page_key)
        if not current or current.get("sha256") != spec.get("replaced_visual_sha256"):
            raise SemanticSceneError("SCENE.VISUAL_PLAN_STALE",
                                     f"第 {spec['page']} 页视觉计划身份与语义替换不一致。")
        current.update({"file": spec["file"], "sha256": spec["sha256"],
                        "source": f"语义场景图：{spec['label']}；{spec['source_page']}；"
                                  f"{spec['license']}",
                        "semantic_scene_key": scene_key})
        for row in visual_plan.get("pages") or []:
            if int(row.get("page", -1)) == int(spec["page"]):
                row["image"] = f"semantic_scene:{scene_key}"
                row["reason"] = "已由具体语义场景照片替换通用示意图"
    bound_keys = ("pages", "images", "particles", "language_support",
                  "keyword_scene_order")
    visual_plan["plan_sha256"] = hashlib.sha256(
        json.dumps({k: visual_plan[k] for k in bound_keys if k in visual_plan},
                   ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return visual_plan
