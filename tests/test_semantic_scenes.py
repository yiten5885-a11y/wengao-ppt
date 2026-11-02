from __future__ import annotations

import hashlib
import io
import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw, PngImagePlugin
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE, MSO_SHAPE_TYPE
from pptx.util import Inches, Pt

from wengao_ppt import semantic_scenes, scene_images
from wengao_ppt.scene_images import (
    SceneImageError,
    OpenversePhotoProvider,
    ScenePhotoProvider,
    WikimediaCommonsPhotoProvider,
    image_hash_distance,
    is_neutral_photo_search_query,
    is_allowed_license,
    is_valid_license_url,
    metadata_relevance,
    normalize_photo_search_terms,
    canonicalize_duplicate_photo_search_terms,
    perceptual_hash,
    photo_style_matches_metadata,
    query_preserves_photo_search_anchors,
    scene_search_query_variants,
    scene_metadata_relevance,
    scene_metadata_matches_query,
)
from wengao_ppt.language_support import (
    SUPPORTED_MAJOR_LANGUAGES, detect_language_hint, language_support_record,
)
from wengao_ppt.scene_model import (
    SceneModelError, classify_external_model_failure, resolve_scene_semantics,
    scene_coverage_audit_prompt, scene_prompt, review_semantic_scene_image,
    scene_visual_identity_sha256, scene_image_review_prompt,
    scene_review_payload_sha256, scene_review_result_sha256,
)


class FakePhotoProvider:
    name = "fixture"
    allowed_hosts = {"images.example.org"}

    def __init__(self, image_records: dict[str, dict], *, reuse_first=False,
                 fail_queries=()):
        self.image_records = image_records
        self.reuse_first = reuse_first
        self.fail_queries = set(fail_queries)
        self.queries = []

    def fetch_one(self, query, *, excluded_sha256=None, excluded_source_keys=None,
                  excluded_perceptual_hashes=None, scene_context=None, min_aspect=None):
        self.queries.append(query)
        if query in self.fail_queries:
            raise SceneImageError("SCENE.NO_LICENSED_PHOTO", "fixture has no licensed match")
        key = "cup" if "mug" in query.lower() or "cup" in query.lower() else "device"
        if self.reuse_first:
            key = "cup"
        record = self.image_records[key]
        data = Path(record["file"]).read_bytes()
        with Image.open(io.BytesIO(data)) as photo:
            actual_width, actual_height = photo.size
        if key == "cup":
            vessel = "mug" if "mug" in query.lower() else "cup"
            title = description = (
                f"A hand holding a ceramic {vessel}, person drinking water at a home kitchen table")
        else:
            title = description = "iPhone Mac work communication at an office desk"
        if self.reuse_first:
            # Keep metadata relevant so this fixture reaches the deck-wide byte
            # dedupe branch instead of being rejected earlier as an unrelated photo.
            title = description = query
        if record.get("metadata_override"):
            title = description = str(record["metadata_override"])
        elif "person drinking water from a ceramic mug" in query.lower():
            title = description = "Person drinking water from a ceramic mug"
        return {
            "provider": "fixture", "source_key": f"fixture:{key}",
            "title": title, "creator": "Fixture artist",
            "source_page": f"https://commons.wikimedia.org/wiki/File:{key}.png",
            "download_url": "https://images.example.org/image.png",
            "license": "CC0 1.0", "license_version": "1.0",
            "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
            "attribution": f"{title} by Fixture artist",
            "description": description, "file": record["file"],
            "sha256": hashlib.sha256(data).hexdigest(),
            "perceptual_hash": perceptual_hash(data), "bytes": len(data),
            "actual_format": "PNG", "actual_width": actual_width, "actual_height": actual_height,
        }


def png_bytes(seed: int, *, metadata: str | None = None) -> bytes:
    image = Image.new("RGB", (800, 600), (18 + seed * 20, 30, 46))
    draw = ImageDraw.Draw(image)
    for i in range(12):
        x = (i * 53 + seed * 17) % 780
        y = (i * 37 + seed * 23) % 580
        draw.rectangle((x, y, min(799, x + 12 + seed), min(599, y + 24)),
                       fill=(190 + (i % 2) * 30, 140 + seed * 5, 80 + i * 5))
    # A downloaded scene photo is photographic, not flat vector art. The
    # provider now rejects flat artwork, so this fixture carries deterministic
    # per-pixel grain and stays representative of the asset it stands in for.
    # Seeds still produce distinguishable bytes for dedupe/near-duplicate tests.
    grain = Image.frombytes(
        "RGB", (800, 600),
        random.Random(seed * 7919).randbytes(800 * 600 * 3))
    image = Image.blend(image, grain, 0.7)
    output = io.BytesIO()
    info = None
    if metadata is not None:
        info = PngImagePlugin.PngInfo()
        info.add_text("fixture", metadata)
    image.save(output, format="PNG", pnginfo=info)
    return output.getvalue()


def make_source_pptx(path: Path, *, full_slide_fill=False) -> Path:
    prs = Presentation()
    for page, body in enumerate([
        "一只手拿着陶瓷杯喝水的场景，说明日常饮水。",
        "一只手拿着陶瓷杯喝水的场景，说明日常饮水。iPhone 与 Mac 用于工作沟通。",
    ], 1):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        if full_slide_fill:
            shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0,
                                           prs.slide_width, prs.slide_height)
            shape.fill.solid()
            shape.fill.fore_color.rgb = __import__("pptx").dml.color.RGBColor(25, 25, 25)
            shape.line.fill.background()
        title = slide.shapes.add_textbox(Inches(.6), Inches(.45), Inches(5.7), Inches(.65))
        title.text_frame.text = f"第 {page} 页主题"
        textbox = slide.shapes.add_textbox(Inches(.6), Inches(1.35), Inches(5.8), Inches(3.2))
        textbox.text_frame.text = body
    prs.save(path)
    return path


def make_compact_source_pptx(path: Path) -> Path:
    prs = Presentation()
    for body in ("一只手拿着陶瓷杯喝水的场景。",
                 "一只手拿着陶瓷杯喝水的场景。iPhone 与 Mac 用于工作沟通。"):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        textbox = slide.shapes.add_textbox(Inches(.6), Inches(.55), Inches(4.4), Inches(.8))
        textbox.text_frame.text = body
    prs.save(path)
    return path


def semantic_fixture():
    cup_quote = "一只手拿着陶瓷杯喝水的场景"
    device_quote = "iPhone 与 Mac 用于工作沟通"
    return {
        "schema_version": "2.2.0",
        "page_coverage": [
            {"page": 1, "scene_keys": ["hand_drinking_from_ceramic_cup"],
             "no_scene_reason": None, "evidence": [cup_quote]},
            {"page": 2, "scene_keys": ["hand_drinking_from_ceramic_cup", "iphone_mac_work_use"],
             "no_scene_reason": None, "evidence": [cup_quote, device_quote]},
        ],
        "scenes": [
            {"scene_key": "hand_drinking_from_ceramic_cup",
             "canonical_scene_id": "hand_drinking_from_ceramic_cup", "topic": "饮水",
             "depiction_type": "illustrative",
             "illustration_rationale": "手持杯子饮水的代表画面能帮助理解日常饮水主题，不表示原文事件真实发生。",
             "subject": "手拿陶瓷杯的人", "action": "喝水", "setting": "日常环境",
             "lighting": "柔和自然晨光", "composition_camera": "侧面中近景",
             "style": "生活纪实摄影", "quality": "清晰自然真实质感",
             "label": "手拿杯饮水", "visual_query": "hand holding ceramic cup drinking water",
             "visual_query_alternatives": [],
             "photo_search_terms": {"subject": "ceramic cup", "action": "drinking water",
                                    "setting": "kitchen"},
             "page_candidates": [
                 {"page": 1, "relevance": .9, "evidence": [cup_quote]},
                 {"page": 2, "relevance": .85, "evidence": [cup_quote]},
             ]},
            {"scene_key": "iphone_mac_work_use", "canonical_scene_id": "iphone_mac_work_use",
             "topic": "电子产品",
             "depiction_type": "illustrative",
             "illustration_rationale": "办公桌上的电子产品能帮助理解设备使用主题，不表示原文事件真实发生。",
             "subject": "iPhone 与 Mac", "action": "工作沟通", "setting": "办公桌",
             "lighting": "柔和办公室环境光", "composition_camera": "桌面斜侧中景",
             "style": "现代商务纪实摄影", "quality": "清晰细节丰富真实质感",
             "label": "iPhone 与 Mac 办公", "visual_query": "iPhone Mac work communication at desk",
             "visual_query_alternatives": [],
             "photo_search_terms": {"subject": "iPhone Mac", "action": "work communication",
                                    "setting": "desk"},
             "page_candidates": [
                 {"page": 2, "relevance": .95, "evidence": [device_quote]},
             ]},
        ],
    }


def fixture_visual_review_call(model: str, scene: dict, image_bytes: bytes) -> dict:
    """Deterministic accepted proof for tests; never invokes a live model."""
    image_sha = hashlib.sha256(image_bytes).hexdigest()
    response_sha = hashlib.sha256(b"fixture visual review result").hexdigest()
    result = {
        "scene_match": True,
        "subject_visible": True,
        "action_visible": True,
        "setting_visible": True,
        "photographic": True,
        "confidence": "high",
        "reason_code": "NONE",
    }
    return {
        "schema_version": "WG-LOCAL-SCENE-IMAGE-REVIEW/1",
        "status": "PASSED",
        "image_sha256": image_sha,
        "visual_input_sha256": scene_review_payload_sha256(image_bytes),
        "scene_identity_sha256": scene_visual_identity_sha256(scene),
        "prompt_sha256": hashlib.sha256(
            scene_image_review_prompt(scene).encode("utf-8")).hexdigest(),
        "review_result_sha256": scene_review_result_sha256(result),
        "model": model,
        "response_sha256": response_sha,
        "elapsed_seconds": 0.01,
        **result,
    }


def synthetic_scene_render_evidence(candidate: Path, plan: dict, output_dir: Path,
                                    *, occlude_fraction: float = 0.0) -> dict:
    """Build deterministic pixel fixtures; this is not a real-render acceptance."""
    output_dir.mkdir(parents=True, exist_ok=True)
    prs = Presentation(str(candidate))
    width = 1280
    height = int(round(width * prs.slide_height / prs.slide_width))
    rendered_pages = []
    rendered_pdf_pages = []
    suppressed_pdf_pages = []
    for page_no, slide in enumerate(prs.slides, 1):
        page = Image.new("RGB", (width, height), (31, 37, 44))
        suppressed_page = Image.new("RGB", (width, height), (31, 37, 44))
        for shape in slide.shapes:
            if getattr(shape, "shape_type", None) != MSO_SHAPE_TYPE.PICTURE:
                continue
            marker = str(shape._element.nvPicPr.cNvPr.get("name") or "")
            if not marker.startswith("WENGAO_SCENE|"):
                continue
            key = marker.split("|", 2)[1]
            left = int(round(shape.left * width / prs.slide_width))
            top = int(round(shape.top * height / prs.slide_height))
            right = int(round((shape.left + shape.width) * width / prs.slide_width))
            bottom = int(round((shape.top + shape.height) * height / prs.slide_height))
            source = Image.open(io.BytesIO(shape.image.blob)).convert("RGB")
            crop = [max(0.0, float(shape.crop_left)), max(0.0, float(shape.crop_top)),
                    max(0.0, float(shape.crop_right)), max(0.0, float(shape.crop_bottom))]
            sw, sh = source.size
            source = source.crop((int(round(crop[0] * sw)), int(round(crop[1] * sh)),
                                  int(round((1 - crop[2]) * sw)),
                                  int(round((1 - crop[3]) * sh))))
            photo = source.resize((right - left, bottom - top), Image.Resampling.LANCZOS)
            page.paste(photo, (left, top))
            if occlude_fraction > 0:
                cover_x = left + int((right - left) * (1.0 - occlude_fraction))
                ImageDraw.Draw(page).rectangle((cover_x, top, right, bottom), fill=(31, 37, 44))
        path = output_dir / f"page-{page_no:02d}.png"
        suppressed_path = output_dir / f"page-{page_no:02d}-scene-suppressed.png"
        page.save(path)
        suppressed_page.save(suppressed_path)
        rendered_pdf_pages.append(page.copy())
        suppressed_pdf_pages.append(suppressed_page.copy())
        rendered_pages.append({
            "page": page_no, "png_path": str(path),
            "png_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "suppressed_png_path": str(suppressed_path),
            "suppressed_png_sha256": hashlib.sha256(suppressed_path.read_bytes()).hexdigest(),
        })
    rendered_pdf = output_dir / "candidate-render.pdf"
    suppressed_pdf = output_dir / "candidate-without-scenes-render.pdf"
    rendered_pdf_pages[0].save(rendered_pdf, format="PDF", save_all=True,
                               append_images=rendered_pdf_pages[1:])
    suppressed_pdf_pages[0].save(suppressed_pdf, format="PDF", save_all=True,
                                 append_images=suppressed_pdf_pages[1:])
    suppressed_pptx = output_dir / "candidate-without-scene-pictures.pptx"
    suppressed_identity = semantic_scenes.make_scene_suppressed_pptx(candidate, suppressed_pptx)
    return {
        "schema": "WG-SEMANTIC-SCENE-RENDER-EVIDENCE/1",
        "candidate_pptx_sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(),
        "scene_suppressed_pptx_sha256": suppressed_identity["scene_suppressed_pptx_sha256"],
        "scene_plan_sha256": plan["plan_sha256"],
        "policy_sha256": semantic_scenes.POLICY_SHA256,
        "rendered_pdf_path": str(rendered_pdf),
        "rendered_pdf_sha256": hashlib.sha256(rendered_pdf.read_bytes()).hexdigest(),
        "suppressed_rendered_pdf_path": str(suppressed_pdf),
        "suppressed_rendered_pdf_sha256": hashlib.sha256(suppressed_pdf.read_bytes()).hexdigest(),
        "renderer": {"name": "synthetic-fixture-rasterizer", "version": "1"},
        "pages": rendered_pages,
    }


class SemanticSceneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wengao-scene-test-")
        self.root = Path(self.temp.name)
        self.source = make_source_pptx(self.root / "source.pptx")
        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.images = {}
        for key, seed, title in (("cup", 1, "Ceramic cup drinking"),
                                 ("device", 3, "iPhone Mac work desk")):
            path = self.assets / f"{key}.png"
            path.write_bytes(png_bytes(seed))
            self.images[key] = {"file": str(path), "title": title}

    def tearDown(self):
        self.temp.cleanup()

    def test_open_vocabulary_prompt_includes_non_exhaustive_user_examples(self):
        prompt = scene_prompt([{"page": 1, "title": "杯具", "text": "保温杯与饮水"}])
        for token in ("消费者", "商超", "发票", "结算台", "生产者", "生产线",
                      "外卖骑手", "陶瓷杯", "保温杯", "手拿杯饮水", "iPhone", "Mac", "病人",
                      "挂号", "病床", "医生", "门诊", "养老院", "公园", "广场舞",
                      "冥想", "图书馆", "书籍", "新闻页", "报纸", "上班族",
                      "工地工人", "地铁", "出租车", "飞机", "单车", "步行",
                      "爬山", "景点", "学生", "课堂", "任何示例都不限制其他题材"):
            self.assertIn(token, prompt)
        self.assertIn("开放词表", prompt)
        self.assertIn("evidence", prompt)
        self.assertIn("visual_query_alternatives", prompt)
        self.assertIn("photo_search_terms", prompt)
        for language in ("Spanish", "French", "German", "Portuguese", "Japanese",
                         "Korean", "Russian", "Arabic", "Hindi", "Indonesian"):
            self.assertIn(language, prompt)
        self.assertIn("source_language_hint", prompt)
        self.assertIn("canonical_scene_id", prompt)
        self.assertIn("neutral English photo-search queries", prompt)
        self.assertIn("illustration_rationale", prompt)
        self.assertIn('"schema_version":"2.2.0"', prompt)
        self.assertIn("抽象概念、统计指标、对比表格", prompt)

    def test_real_semantic_plan_requires_a_second_full_page_coverage_audit(self):
        pages = semantic_scenes._source_pages(self.source)
        resolution = semantic_fixture()
        audit = {
            "schema_version": "1.0.0", "review_status": "NO_GAPS",
            "page_reviews": [
                {"page": row["page"], "coverage_status": "SCENE_COVERED",
                 "scene_keys": row["scene_keys"],
                 "reason": "已逐页复核并覆盖该页所有具体场景语义。",
                 "evidence": [pages[row["page"] - 1]["text"].split("\n")[0]]}
                for row in resolution["page_coverage"]
            ],
            "missing_scenes": [], "merged_distinct_scenes": [],
        }
        calls = []

        def model_call(model, prompt):
            calls.append(prompt)
            payload = resolution if len(calls) == 1 else audit
            return {"response": json.dumps(payload, ensure_ascii=False),
                    "model": "fixture-local-model", "elapsed_seconds": 0.01}

        resolved, meta = resolve_scene_semantics(pages, model_call=model_call)
        self.assertEqual(len(calls), 2)
        self.assertIn("independent second-pass semantic coverage auditor", calls[1])
        self.assertEqual(meta["coverage_audit"]["status"], "PASSED")
        self.assertEqual(meta["coverage_audit"]["pages_checked"], len(pages))
        self.assertEqual(len(resolved["scenes"]), 2)
        self.assertEqual(resolved["scenes"][0]["image_plan"]["provider_mode"], "photo_search")
        self.assertTrue(all(resolved["scenes"][0][field] for field in (
            "subject", "environment", "action_state", "lighting",
            "composition_camera", "style", "quality")))

        calls.clear()
        generated_plan = semantic_scenes.build_plan(
            self.source, provider=FakePhotoProvider(self.images), model_call=model_call,
            visual_review_call=fixture_visual_review_call)
        plan_path = self.root / "model-generated-plan.json"
        plan_path.write_text(json.dumps(generated_plan, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(generated_plan["coverage_audit"]["status"], "PASSED")
        self.assertEqual(semantic_scenes._validate_coverage_audit_proof(
            self.source, plan_path)["status"], "EXECUTED")

    def test_dangling_page_coverage_scene_link_is_reconciled_from_its_own_evidence(self):
        """page_coverage.scene_keys can reference a scene whose own
        page_candidates never got a matching entry for that page — the model
        wrote the two redundant records inconsistently on the very first pass,
        with no merge/gap repair involved at all. resolve_scene_semantics must
        close that gap using the page_coverage row's own (already-grounded)
        evidence, not leave it for build_plan's unforgiving structural check
        to crash on with zero repair chance.
        """
        pages = semantic_scenes._source_pages(self.source)
        resolution = semantic_fixture()
        cup_key = resolution["scenes"][0]["scene_key"]
        self.assertEqual(cup_key, "hand_drinking_from_ceramic_cup")
        # page_coverage still claims the cup scene for page 2, but strip page 2
        # out of that scene's own page_candidates — the exact mismatch shape
        # captured live.
        resolution["scenes"][0]["page_candidates"] = [
            c for c in resolution["scenes"][0]["page_candidates"] if c["page"] != 2]
        audit = {
            "schema_version": "1.0.0", "review_status": "NO_GAPS",
            "page_reviews": [
                {"page": row["page"], "coverage_status": "SCENE_COVERED",
                 "scene_keys": row["scene_keys"],
                 "reason": "已逐页复核并覆盖该页所有具体场景语义。",
                 "evidence": [pages[row["page"] - 1]["text"].split("\n")[0]]}
                for row in resolution["page_coverage"]
            ],
            "missing_scenes": [], "merged_distinct_scenes": [],
        }
        calls = []

        def model_call(model, prompt):
            calls.append(prompt)
            payload = resolution if len(calls) == 1 else audit
            return {"response": json.dumps(payload, ensure_ascii=False),
                    "model": "fixture-local-model", "elapsed_seconds": 0.01}

        resolved, meta = resolve_scene_semantics(pages, model_call=model_call)
        self.assertEqual(meta["coverage_audit"]["status"], "PASSED")
        cup = next(s for s in resolved["scenes"] if s["scene_key"] == cup_key)
        self.assertIn(2, cup["candidate_pages"])
        self.assertIn(2, [c["page"] for c in cup["page_candidates"]])
        reconciled = next(c for c in cup["page_candidates"] if c["page"] == 2)
        # The copied evidence must be exactly the page_coverage row's own
        # (already-grounded) evidence — never invented.
        self.assertEqual(reconciled["evidence"],
                         resolution["page_coverage"][1]["evidence"])

        calls.clear()
        generated_plan = semantic_scenes.build_plan(
            self.source, provider=FakePhotoProvider(self.images), model_call=model_call,
            visual_review_call=fixture_visual_review_call)
        plan_path = self.root / "reconciled-link-plan.json"
        plan_path.write_text(json.dumps(generated_plan, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(generated_plan["coverage_audit"]["status"], "PASSED")
        # The reconciliation must happen before the audit's own frozen
        # plan-view snapshot, not after — otherwise this independent replay
        # check flags it as unexplained drift (SCENE.COVERAGE_AUDIT_PLAN_DRIFT)
        # instead of the link actually being fixed. This is the exact check
        # that caught the reconciliation running too late in production.
        proof = semantic_scenes._validate_coverage_audit_proof(self.source, plan_path)
        self.assertEqual(proof["status"], "EXECUTED", proof)

    def test_invalid_photo_anchors_get_one_same_model_grounded_format_repair(self):
        pages = semantic_scenes._source_pages(self.source)
        resolution = semantic_fixture()
        resolution["scenes"][0]["photo_search_terms"] = {
            "subject": "people", "action": "drinking", "setting": "place"}
        audit = {
            "schema_version": "1.0.0", "review_status": "NO_GAPS",
            "page_reviews": [
                {"page": row["page"], "coverage_status": "SCENE_COVERED",
                 "scene_keys": row["scene_keys"],
                 "reason": "独立复核原文支持的场景覆盖。",
                 "evidence": [pages[row["page"] - 1]["text"].split("\n")[0]]}
                for row in resolution["page_coverage"]
            ],
            "missing_scenes": [], "merged_distinct_scenes": [],
        }
        repair = {"repairs": [{
            "scene_key": "hand_drinking_from_ceramic_cup",
            "photo_search_terms": {"subject": "person", "action": "person drinking water",
                                   "setting": "kitchen"},
        }]}
        calls = []

        def model_call(model, prompt):
            calls.append((model, prompt))
            if len(calls) == 1:
                payload = resolution
            elif "correcting only the English photo-search anchor formatting" in prompt:
                self.assertIn("一只手拿着陶瓷杯喝水的场景", prompt)
                payload = repair
            else:
                payload = audit
            return {"response": json.dumps(payload, ensure_ascii=False),
                    "model": model, "elapsed_seconds": 0.01}

        resolved, meta = resolve_scene_semantics(
            pages, model="fixture-local-model", model_call=model_call)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(model == "fixture-local-model" for model, _ in calls))
        self.assertIn("independent second-pass semantic coverage auditor", calls[2][1])
        self.assertEqual(resolved["scenes"][0]["photo_search_terms"],
                         {"subject": "person", "action": "drinking water",
                          "setting": "kitchen"})
        repair_meta = meta["photo_search_anchor_repairs"][0]
        self.assertEqual(repair_meta["status"], "APPLIED_AND_STRICTLY_VALIDATED")
        self.assertEqual(repair_meta["scenes_repaired"], 1)
        self.assertTrue(repair_meta["same_model"])
        self.assertEqual(repair_meta["canonicalized_scene_count"], 1)
        self.assertEqual(repair_meta["repair_method"],
                         "same_model_source_scoped_anchor_format_repair")
        self.assertRegex(repair_meta["canonicalization_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(repair_meta["prompt_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(repair_meta["response_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(repair_meta["result_sha256"], r"^[0-9a-f]{64}$")

    def test_duplicate_photo_anchor_tokens_are_removed_only_when_strict_result_still_passes(self):
        source = {"subject": "worker", "action": "worker assembling parts",
                  "setting": "factory floor"}
        with self.assertRaisesRegex(ValueError, "must be distinct"):
            normalize_photo_search_terms(source)
        self.assertEqual(canonicalize_duplicate_photo_search_terms(source),
                         {"subject": "worker", "action": "assembling parts",
                          "setting": "factory floor"})
        unsafe = {"subject": "worker", "action": "worker assembling",
                  "setting": "factory floor"}
        self.assertIsNone(canonicalize_duplicate_photo_search_terms(unsafe))

    def test_invalid_photo_anchor_repair_still_blocks_when_strict_schema_fails(self):
        pages = semantic_scenes._source_pages(self.source)
        resolution = semantic_fixture()
        resolution["scenes"][0]["photo_search_terms"] = {
            "subject": "people", "action": "drinking", "setting": "place"}
        calls = []

        def model_call(model, prompt):
            calls.append(prompt)
            if len(calls) == 1:
                payload = resolution
            elif "correcting only the English photo-search anchor formatting" in prompt:
                payload = {"repairs": [{
                    "scene_key": "hand_drinking_from_ceramic_cup",
                    "photo_search_terms": {"subject": "person", "action": "drink",
                                           "setting": "kitchen"},
                }]}
            else:
                self.fail("invalid anchor repair must block before the audit")
            return {"response": json.dumps(payload, ensure_ascii=False),
                    "model": model, "elapsed_seconds": 0.01}

        with self.assertRaises(SceneModelError) as caught:
            resolve_scene_semantics(
                pages, model="fixture-local-model", model_call=model_call)
        self.assertEqual(caught.exception.code, "SCENE.PHOTO_SEARCH_ANCHORS_INVALID")
        # One original call plus two bounded same-model repair attempts
        # (local models occasionally miss the strict anchor format once).
        self.assertEqual(len(calls), 3)
        self.assertTrue(caught.exception.diagnostics["same_model"])
        self.assertEqual(caught.exception.diagnostics["repair_attempts"], 2)
        self.assertRegex(caught.exception.diagnostics["repair_response_sha256"],
                         r"^[0-9a-f]{64}$")

    def test_audited_distinct_scene_merge_gets_one_bounded_replan_and_third_audit(self):
        pages = semantic_scenes._source_pages(self.source)
        cup_quote = "一只手拿着陶瓷杯喝水的场景，说明日常饮水。"
        device_quote = "iPhone 与 Mac 用于工作沟通。"
        mixed = {
            "schema_version": "2.2.0",
            "page_coverage": [
                {"page": 1, "scene_keys": ["mixed_daily_activity"],
                 "no_scene_reason": None, "evidence": [cup_quote]},
                {"page": 2, "scene_keys": ["mixed_daily_activity"],
                 "no_scene_reason": None, "evidence": [cup_quote, device_quote]},
            ],
            "scenes": [{
                "scene_key": "mixed_daily_activity",
                "canonical_scene_id": "mixed_daily_activity",
                "topic": "日常生活", "subject": "手和电子产品",
                "depiction_type": "illustrative",
                "illustration_rationale": "手持杯子饮水代表日常饮水，电子设备沟通代表工作使用；图片不表示原文事件发生。",
                "action": "喝水和工作沟通", "setting": "日常环境",
                "label": "日常活动", "visual_query": "person drinking water kitchen",
                "photo_search_terms": {"subject": "person", "action": "drinking water",
                                       "setting": "kitchen"},
                "visual_query_alternatives": [],
                "lighting": "soft natural daylight",
                "composition_camera": "documentary medium view",
                "style": "realistic lifestyle photography",
                "quality": "sharp natural detail",
                "page_candidates": [
                    {"page": 1, "relevance": .9, "evidence": [cup_quote]},
                    {"page": 2, "relevance": .9, "evidence": [device_quote]},
                ],
            }],
        }
        merge_audit = {
            "schema_version": "1.2.0", "review_status": "GAPS_FOUND",
            "page_reviews": [
                {"page": 1, "coverage_status": "SCENE_COVERED",
                 "scene_keys": ["mixed_daily_activity"]},
                {"page": 2, "coverage_status": "SCENE_COVERED",
                 "scene_keys": ["mixed_daily_activity"]},
            ],
            "missing_scenes": [],
            "merged_distinct_scenes": [{
                "pages": [1, 2], "scene_keys": ["mixed_daily_activity"],
                "reason": "喝水与电子设备工作沟通是不同的具体场景。",
            }],
        }
        replanned = semantic_fixture()
        merge_patch = {
            "schema_version": "1.0.0",
            "page_scene_replacements": [
                {"page": row["page"], "scene_keys": list(row["scene_keys"])}
                for row in replanned["page_coverage"]
            ],
            "scenes": replanned["scenes"],
        }
        final_audit = {
            "schema_version": "1.2.0", "review_status": "NO_GAPS",
            "page_reviews": [
                {"page": row["page"], "coverage_status": "SCENE_COVERED",
                 "scene_keys": list(row["scene_keys"])}
                for row in replanned["page_coverage"]
            ],
            "missing_scenes": [], "merged_distinct_scenes": [],
        }
        calls = []

        def model_call(model, prompt):
            calls.append(prompt)
            payload = (mixed if len(calls) == 1 else
                       merge_audit if len(calls) == 2 else
                       merge_patch if len(calls) == 3 else final_audit)
            return {"response": json.dumps(payload, ensure_ascii=False),
                    "model": "fixture-local-model", "elapsed_seconds": .01}

        resolved, meta = resolve_scene_semantics(
            pages, model="fixture-local-model", model_call=model_call)
        self.assertEqual(len(calls), 4)
        self.assertIn("independent second-pass semantic coverage auditor", calls[1])
        self.assertIn("constrained multilingual semantic-scene conflict patcher", calls[2])
        self.assertIn("page_scene_replacements", calls[2])
        self.assertNotIn("Return a complete replacement plan", calls[2])
        self.assertIn("post-repair semantic coverage verifier", calls[3])
        self.assertEqual(meta["coverage_audit"]["status"], "PASSED")
        repair = meta["coverage_audit"]["gap_repair"]
        self.assertEqual(repair["kind"], "merge_replan_patches")
        self.assertEqual(repair["status"], "APPLIED_AND_REVERIFIED")
        self.assertEqual(len(resolved["scenes"]), 2)
        cup = next(row for row in resolved["scenes"]
                   if row["scene_key"] == "hand_drinking_from_ceramic_cup")
        self.assertEqual([row["page"] for row in cup["page_candidates"]], [1, 2])

        normalized_scenes, normalized_coverage = semantic_scenes._normalize_semantics(
            resolved, pages, require_canonical_scene_id=True)
        frozen = {
            "scenes": normalized_scenes,
            "page_coverage": normalized_coverage,
            "coverage_audit": meta["coverage_audit"],
        }
        plan_path = self.root / "merge-replan-proof.json"
        plan_path.write_text(json.dumps(frozen, ensure_ascii=False), encoding="utf-8")
        proof_check = semantic_scenes._validate_coverage_audit_proof(
            self.source, plan_path)
        self.assertEqual(proof_check["status"], "EXECUTED", proof_check)

    def test_malformed_audit_json_gets_one_same_model_format_retry(self):
        pages = semantic_scenes._source_pages(self.source)
        resolution = semantic_fixture()
        audit = {
            "schema_version": "1.0.0", "review_status": "NO_GAPS",
            "page_reviews": [
                {"page": row["page"], "coverage_status": "SCENE_COVERED",
                 "scene_keys": row["scene_keys"],
                 "reason": "已逐页复核并覆盖该页所有具体场景语义。",
                 "evidence": row["evidence"]}
                for row in resolution["page_coverage"]
            ],
            "missing_scenes": [], "merged_distinct_scenes": [],
        }
        calls = []

        def model_call(model, prompt):
            calls.append((model, prompt))
            if len(calls) == 1:
                payload = resolution
            elif len(calls) == 2:
                return {"response": "{malformed", "model": "fixture-local-model"}
            else:
                payload = audit
            return {"response": json.dumps(payload, ensure_ascii=False),
                    "model": "fixture-local-model", "elapsed_seconds": 0.01}

        resolved, meta = resolve_scene_semantics(
            pages, model="fixture-local-model", model_call=model_call)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(model == "fixture-local-model" for model, _ in calls))
        self.assertIn("格式修复重试", calls[2][1])
        self.assertEqual(meta["coverage_audit"]["status"], "PASSED")
        retry = meta["coverage_audit"]["json_format_retry"]
        self.assertEqual(retry["attempts"], 2)
        self.assertEqual(retry["first_error_code"], "SCENE.MODEL_JSON_INVALID")
        self.assertTrue(retry["same_model"])
        self.assertFalse(retry["source_response_replayed"])
        self.assertEqual(len(resolved["scenes"]), 2)

    def test_malformed_json_retry_still_fails_closed_after_one_retry(self):
        pages = semantic_scenes._source_pages(self.source)
        calls = []

        def model_call(model, prompt):
            calls.append(prompt)
            return {"response": "not-json", "model": "fixture-local-model"}

        with self.assertRaises(SceneModelError) as caught:
            resolve_scene_semantics(pages, model_call=model_call)
        self.assertEqual(caught.exception.code, "SCENE.MODEL_JSON_INVALID")
        self.assertEqual(len(calls), 2)
        self.assertIn("格式修复重试", calls[1])
        self.assertEqual(caught.exception.diagnostics["json_format_retry"]["attempts"], 2)
        self.assertTrue(caught.exception.diagnostics["json_format_retry"]["same_model"])

    def test_model_quota_error_is_not_retried_as_a_json_format_problem(self):
        pages = semantic_scenes._source_pages(self.source)
        calls = []

        def model_call(model, prompt):
            calls.append((model, prompt))
            raise SceneModelError("MODEL.QUOTA_EXCEEDED", "fixture quota error")

        with self.assertRaises(SceneModelError) as caught:
            resolve_scene_semantics(pages, model_call=model_call)
        self.assertEqual(caught.exception.code, "MODEL.QUOTA_EXCEEDED")
        self.assertEqual(len(calls), 1)

    def test_real_scene_prompt_rejects_missing_structured_image_prompt_fields(self):
        # composition_camera/style/quality now get one same-model repair
        # attempt (_repair_missing_image_prompt_fields) before this gate
        # raises — mirroring the existing photo_search_terms repair — so a
        # model_call stub that cannot produce a valid {"repairs": [...]}
        # shape (as here) still correctly blocks, just with the repair's own
        # "both attempts failed validation" message and the extra repair
        # calls counted in.
        pages = semantic_scenes._source_pages(self.source)
        resolution = semantic_fixture()
        resolution["scenes"][0].pop("composition_camera")
        calls = []

        def model_call(model, prompt):
            calls.append(prompt)
            if '"repairs"' in prompt:
                return {"response": '{"repairs":[]}',
                        "model": "fixture-local-model", "elapsed_seconds": 0.01}
            return {"response": json.dumps(resolution, ensure_ascii=False),
                    "model": "fixture-local-model", "elapsed_seconds": 0.01}

        with self.assertRaises(SceneModelError) as caught:
            resolve_scene_semantics(pages, model_call=model_call)
        self.assertEqual(caught.exception.code, "SCENE.IMAGE_PROMPT_SCHEMA_MISSING")
        self.assertEqual(len(calls), 3,
                         "one plan call plus two same-model repair attempts, then block")

    def test_second_pass_blocks_omitted_consumer_scene_not_present_in_plan(self):
        source = Presentation(str(self.source))
        body = next(shape for shape in source.slides[1].shapes
                    if getattr(shape, "has_text_frame", False)
                    and "iPhone" in (shape.text_frame.text or ""))
        body.text_frame.text += "消费者在商超挑选商品并在结算台付款。"
        expanded_source = self.root / "consumer-context-source.pptx"
        source.save(expanded_source)
        pages = semantic_scenes._source_pages(expanded_source)
        resolution = semantic_fixture()
        quote = "消费者在商超挑选商品并在结算台付款"
        audit = {
            "schema_version": "1.0.0", "review_status": "GAPS_FOUND",
            "page_reviews": [
                {"page": 1, "coverage_status": "SCENE_COVERED",
                 "scene_keys": ["hand_drinking_from_ceramic_cup"],
                 "reason": "已覆盖该页饮水场景。", "evidence": ["一只手拿着陶瓷杯喝水的场景"]},
                {"page": 2, "coverage_status": "SCENE_COVERED",
                 "scene_keys": ["hand_drinking_from_ceramic_cup", "iphone_mac_work_use"],
                 "reason": "现有场景尚未覆盖商超结算语义。", "evidence": [quote]},
            ],
            "missing_scenes": [{
                "page": 2, "scene_key": "consumer_supermarket_checkout",
                "canonical_scene_id": "consumer_supermarket_checkout_payment",
                "subject": "消费者", "action": "挑选商品并结算", "setting": "商超结算台",
                "evidence": quote,
            }],
            "merged_distinct_scenes": [],
        }
        calls = []

        def model_call(model, prompt):
            calls.append(prompt)
            payload = resolution if len(calls) == 1 else audit
            return json.dumps(payload, ensure_ascii=False)

        with self.assertRaises(SceneModelError) as caught:
            resolve_scene_semantics(pages, model_call=model_call)
        self.assertEqual(len(calls), 3)
        self.assertIn("constrained local semantic-scene gap-record repairer", calls[2])
        self.assertEqual(caught.exception.code, "SCENE.COVERAGE_AUDIT_GAPS")

    def test_grounded_gap_repair_requires_and_binds_third_full_deck_verification(self):
        source = make_compact_source_pptx(self.root / "same-scene-two-pages.pptx")
        prs = Presentation(str(source))
        quote = "一只手拿着陶瓷杯喝水的场景"
        for slide in prs.slides:
            next(shape for shape in slide.shapes
                 if getattr(shape, "has_text_frame", False)).text_frame.text = quote
        prs.save(source)
        pages = semantic_scenes._source_pages(source)

        resolution = semantic_fixture()
        cup = dict(resolution["scenes"][0])
        cup["page_candidates"] = [{"page": 1, "relevance": .9, "evidence": [quote]}]
        resolution = {
            "schema_version": "2.2.0",
            "page_coverage": [
                {"page": 1, "scene_keys": [cup["scene_key"]],
                 "no_scene_reason": None, "evidence": [quote]},
                {"page": 2, "scene_keys": [],
                 "no_scene_reason": "该页暂无已登记的具体场景，等待独立全稿复核。",
                 "evidence": [quote]},
            ],
            "scenes": [cup],
        }
        gap_scene = {
            "page": 2, "scene_key": cup["scene_key"],
            "canonical_scene_id": cup["canonical_scene_id"],
            "topic": cup["topic"], "subject": cup["subject"],
            "depiction_type": "illustrative",
            "illustration_rationale": "手持杯子饮水的代表画面能帮助理解日常饮水主题，不表示原文事件真实发生。",
            "action": cup["action"], "setting": cup["setting"],
            "label": cup["label"], "visual_query": cup["visual_query"],
            "photo_search_terms": cup["photo_search_terms"],
            "visual_query_alternatives": ["person drinking from ceramic cup"],
            "lighting": cup["lighting"],
            "composition_camera": cup["composition_camera"],
            "style": cup["style"], "quality": cup["quality"],
            "evidence": quote,
        }
        bad_gap_scene = dict(gap_scene)
        bad_gap_scene["photo_search_terms"] = {
            "subject": "worker", "action": "inspecting production line",
            "setting": "factory line",
        }
        initial_audit = {
            "schema_version": "1.3.0", "review_status": "GAPS_FOUND",
            "page_reviews": [
                {"page": 1, "coverage_status": "SCENE_COVERED",
                 "scene_keys": [cup["scene_key"]]},
                {"page": 2, "coverage_status": "NO_SCENE", "scene_keys": []},
            ],
            "missing_scenes": [bad_gap_scene], "merged_distinct_scenes": [],
        }
        gap_repair_response = {
            "schema_version": "1.0.0", "missing_scenes": [gap_scene],
        }
        final_audit = {
            "schema_version": "1.3.0", "review_status": "NO_GAPS",
            "page_reviews": [
                {"page": 1, "coverage_status": "SCENE_COVERED",
                 "scene_keys": [cup["scene_key"]]},
                {"page": 2, "coverage_status": "SCENE_COVERED",
                 "scene_keys": [cup["scene_key"]]},
            ],
            "missing_scenes": [], "merged_distinct_scenes": [],
        }
        calls = []

        def model_call(model, prompt):
            calls.append(prompt)
            payload = (resolution if len(calls) == 1 else
                       initial_audit if len(calls) == 2 else
                       gap_repair_response if len(calls) == 3 else final_audit)
            return {"response": json.dumps(payload, ensure_ascii=False),
                    "model": "fixture-local-model", "elapsed_seconds": .01}

        plan = semantic_scenes.build_plan(
            source, provider=FakePhotoProvider(self.images), model_call=model_call,
            visual_review_call=fixture_visual_review_call)
        self.assertEqual(len(calls), 4)
        self.assertIn("second-pass semantic coverage auditor", calls[1])
        self.assertIn("constrained local semantic-scene gap-record repairer", calls[2])
        self.assertIn("post-repair semantic coverage verifier", calls[3])
        proof = plan["coverage_audit"]
        self.assertEqual(proof["status"], "PASSED")
        self.assertEqual(proof["phase"], "post_repair")
        self.assertEqual(proof["gap_repair"]["status"], "APPLIED_AND_REVERIFIED")
        self.assertEqual(proof["gap_repair"]["repaired_scene_count"], 1)
        self.assertRegex(proof["gap_repair"]["gap_repair_prompt_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(proof["gap_repair"]["gap_repair_response_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(len(plan["scenes"]), 1,
                         "one concrete repeated scene must stay one deduplicated scene record")
        self.assertEqual([row["page"] for row in plan["scenes"][0]["page_candidates"]], [1, 2])
        self.assertEqual(len(plan["images"]), 1,
                         "the repeated scene must be fetched/embedded once, not once per page")
        plan_path = self.root / "same-scene-two-pages-plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        result = semantic_scenes._validate_coverage_audit_proof(source, plan_path)
        self.assertEqual(result["status"], "EXECUTED", result)

        # The final gate independently replays the exact deterministic delta;
        # a post-plan edit cannot reuse the old successful repair proof.
        tampered = json.loads(json.dumps(plan, ensure_ascii=False))
        tampered["scenes"][0]["setting"] = "unrelated supermarket checkout"
        tampered_path = self.root / "same-scene-two-pages-plan-tampered.json"
        tampered_path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
        tampered_result = semantic_scenes._validate_coverage_audit_proof(source, tampered_path)
        self.assertEqual(tampered_result["status"], "FAILED", tampered_result)
        self.assertIn("SCENE.COVERAGE_AUDIT_REPAIR_PROOF_INVALID",
                      {row["code"] for row in tampered_result["findings"]})

    def test_post_repair_verification_disagreement_gets_its_own_repair_and_replay(self):
        """A second, independent audit finding its OWN missing page after the
        first repair must get the same repair chance as the first finding —
        and the whole chain must still replay deterministically at validate
        time. Mirrors test_grounded_gap_repair_requires_and_binds_third_full_deck_verification
        but the post-repair verification audit itself reports GAPS_FOUND for a
        page the initial audit never flagged, forcing a second grounded gap
        repair chained from the first repair's own output.
        """
        source = self.root / "three-page-second-opinion.pptx"
        prs = Presentation()
        for body in ("一只手拿着陶瓷杯喝水的场景。",
                     "iPhone 与 Mac 用于工作沟通。",
                     "一盆绿植摆在窗边安静生长。"):
            slide = prs.slides.add_slide(prs.slide_layouts[6])
            textbox = slide.shapes.add_textbox(Inches(.6), Inches(.55), Inches(4.4), Inches(.8))
            textbox.text_frame.text = body
        prs.save(source)
        pages = semantic_scenes._source_pages(source)

        cup_quote, device_quote, plant_quote = (
            "一只手拿着陶瓷杯喝水的场景", "iPhone 与 Mac 用于工作沟通", "一盆绿植摆在窗边安静生长")
        cup = dict(semantic_fixture()["scenes"][0])
        cup["page_candidates"] = [{"page": 1, "relevance": .9, "evidence": [cup_quote]}]
        resolution = {
            "schema_version": "2.2.0",
            "page_coverage": [
                {"page": 1, "scene_keys": [cup["scene_key"]],
                 "no_scene_reason": None, "evidence": [cup_quote]},
                {"page": 2, "scene_keys": [],
                 "no_scene_reason": "该页暂无已登记的具体场景，等待独立全稿复核。",
                 "evidence": [device_quote]},
                {"page": 3, "scene_keys": [],
                 "no_scene_reason": "该页暂无已登记的具体场景，等待独立全稿复核。",
                 "evidence": [plant_quote]},
            ],
            "scenes": [cup],
        }

        def gap_scene(page, scene_key, quote, subject, action, setting, label, visual_query):
            return {
                "page": page, "scene_key": scene_key, "canonical_scene_id": scene_key,
                "topic": label, "depiction_type": "illustrative",
                "illustration_rationale": f"{label}的代表画面能帮助理解该页主题，不表示原文事件真实发生。",
                "subject": subject, "action": action, "setting": setting,
                "label": label, "visual_query": visual_query,
                "photo_search_terms": {"subject": subject, "action": action, "setting": setting},
                "visual_query_alternatives": [],
                "lighting": "柔和自然光", "composition_camera": "中近景",
                "style": "生活纪实摄影", "quality": "清晰自然真实质感",
                "evidence": quote,
            }

        device_gap = gap_scene(2, "iphone_mac_work_use", device_quote,
                               "iPhone Mac", "work communication", "desk",
                               "iPhone 与 Mac 办公", "iPhone Mac work communication desk")
        plant_gap = gap_scene(3, "potted_plant_by_window", plant_quote,
                              "potted plant", "growing quietly", "windowsill",
                              "窗边绿植", "potted plant growing quietly windowsill")

        initial_audit = {
            "schema_version": "1.3.0", "review_status": "GAPS_FOUND",
            "page_reviews": [
                {"page": 1, "coverage_status": "SCENE_COVERED", "scene_keys": [cup["scene_key"]]},
                {"page": 2, "coverage_status": "NO_SCENE", "scene_keys": []},
                {"page": 3, "coverage_status": "NO_SCENE", "scene_keys": []},
            ],
            "missing_scenes": [device_gap], "merged_distinct_scenes": [],
        }
        # The post-repair verification is a fresh, independent audit call: it
        # agrees page 2 is now covered but — unlike the first audit — also
        # flags page 3 as needing a scene. This is the real disagreement this
        # test exercises, not a second attempt at the same finding.
        verification_audit_with_new_gap = {
            "schema_version": "1.3.0", "review_status": "GAPS_FOUND",
            "page_reviews": [
                {"page": 1, "coverage_status": "SCENE_COVERED", "scene_keys": [cup["scene_key"]]},
                {"page": 2, "coverage_status": "SCENE_COVERED", "scene_keys": [device_gap["scene_key"]]},
                {"page": 3, "coverage_status": "NO_SCENE", "scene_keys": []},
            ],
            "missing_scenes": [plant_gap], "merged_distinct_scenes": [],
        }
        final_audit = {
            "schema_version": "1.3.0", "review_status": "NO_GAPS",
            "page_reviews": [
                {"page": 1, "coverage_status": "SCENE_COVERED", "scene_keys": [cup["scene_key"]]},
                {"page": 2, "coverage_status": "SCENE_COVERED", "scene_keys": [device_gap["scene_key"]]},
                {"page": 3, "coverage_status": "SCENE_COVERED", "scene_keys": [plant_gap["scene_key"]]},
            ],
            "missing_scenes": [], "merged_distinct_scenes": [],
        }
        calls = []

        def model_call(model, prompt):
            calls.append(prompt)
            payload = (resolution if len(calls) == 1 else
                       initial_audit if len(calls) == 2 else
                       verification_audit_with_new_gap if len(calls) == 3 else
                       final_audit)
            return {"response": json.dumps(payload, ensure_ascii=False),
                    "model": "fixture-local-model", "elapsed_seconds": .01}

        images = dict(self.images)
        images["plant"] = {"file": self.images["device"]["file"], "title": "Potted plant windowsill"}
        plan = semantic_scenes.build_plan(
            source, provider=FakePhotoProvider(images), model_call=model_call,
            visual_review_call=fixture_visual_review_call)
        self.assertEqual(len(calls), 4)
        self.assertIn("second-pass semantic coverage auditor", calls[1])
        self.assertIn("post-repair semantic coverage verifier", calls[2])
        self.assertIn("post-repair semantic coverage verifier", calls[3])

        proof = plan["coverage_audit"]
        self.assertEqual(proof["status"], "PASSED")
        gap_repair = proof["gap_repair"]
        self.assertEqual(gap_repair["status"], "APPLIED_AND_REVERIFIED")
        self.assertEqual(gap_repair["repaired_scene_count"], 1)
        second = gap_repair["second_gap_repair"]
        self.assertEqual(second["kind"], "post_verification_gap_repair")
        self.assertEqual(second["repaired_scene_count"], 1)
        self.assertEqual(second["repaired_scene_keys"], ["potted_plant_by_window"])
        self.assertRegex(second["prompt_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(second["result_sha256"], r"^[0-9a-f]{64}$")

        scene_keys = {row["scene_key"] for row in plan["scenes"]}
        self.assertEqual(scene_keys, {cup["scene_key"], "iphone_mac_work_use",
                                      "potted_plant_by_window"})

        plan_path = self.root / "three-page-second-opinion-plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        result = semantic_scenes._validate_coverage_audit_proof(source, plan_path)
        self.assertEqual(result["status"], "EXECUTED", result)

        # Tampering the SECOND repair's own output must still be caught by replay.
        tampered = json.loads(json.dumps(plan, ensure_ascii=False))
        tampered["scenes"] = [
            (dict(row, setting="unrelated supermarket checkout")
             if row["scene_key"] == "potted_plant_by_window" else row)
            for row in tampered["scenes"]
        ]
        tampered_path = self.root / "three-page-second-opinion-plan-tampered.json"
        tampered_path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
        tampered_result = semantic_scenes._validate_coverage_audit_proof(source, tampered_path)
        self.assertEqual(tampered_result["status"], "FAILED", tampered_result)
        self.assertIn("SCENE.COVERAGE_AUDIT_REPAIR_PROOF_INVALID",
                      {row["code"] for row in tampered_result["findings"]})

    def test_portrait_scene_blocks_an_ultrawide_slot_instead_of_cropping_or_using_a_card(self):
        portrait_path = self.assets / "portrait-factory-worker.png"
        portrait = Image.new("RGB", (600, 1000), (26, 80, 120))
        ImageDraw.Draw(portrait).rectangle((180, 80, 420, 920), fill=(230, 180, 80))
        portrait.save(portrait_path)
        self.images["cup"] = {"file": str(portrait_path), "title": "Ceramic cup drinking"}
        placeholder_path = self.assets / "ultrawide-placeholder.png"
        placeholder_path.write_bytes(png_bytes(7))
        source_with_slot = Presentation(str(self.source))
        slide = source_with_slot.slides[0]
        for shape in list(slide.shapes):
            shape._element.getparent().remove(shape._element)
        source_text = "一只手拿着陶瓷杯喝水的场景"
        text = slide.shapes.add_textbox(Inches(0.72), Inches(0.55), Inches(11.9), Inches(0.7))
        text.text_frame.text = source_text
        slot_rect = [0.72, 5.66, 11.95, 1.22]
        source_with_slot.slides[0].shapes.add_picture(
            str(placeholder_path), Inches(slot_rect[0]), Inches(slot_rect[1]),
            Inches(slot_rect[2]), Inches(slot_rect[3]))
        slot_source = self.root / "source-with-ultrawide-slot.pptx"
        source_with_slot.save(slot_source)
        placeholder_sha = hashlib.sha256(placeholder_path.read_bytes()).hexdigest()
        visual_plan = {"images": {"1": {"page": 1, "rect": slot_rect,
                                         "sha256": placeholder_sha,
                                         "file": str(placeholder_path)}}}

        plan = semantic_scenes.build_plan(
            slot_source, visual_plan=visual_plan,
            provider=FakePhotoProvider(self.images),
            semantic_resolution=semantic_fixture())
        self.assertNotIn("hand_drinking_from_ceramic_cup", plan["images"])
        # 版位放不下是如实登记的"未放置"，不是阻断交付的未解决缺口。
        self.assertIn("SCENE.NO_SAFE_PHOTO_AREA",
                      {row["reason"] for row in plan["unplaced_scenes"]})
        self.assertEqual(plan["unresolved_scenes"], [])

    def test_offline_language_hints_cover_major_scripts_and_latin_languages(self):
        samples = {
            "zh": "消费者在结算台付款。",
            "en": "The patient visits the clinic and meets a doctor for care.",
            "es": "El paciente visita la clínica y habla con el médico para recibir atención.",
            "fr": "Le patient visite la clinique et parle avec le médecin pour les soins.",
            "de": "Der Patient besucht die Klinik und spricht mit dem Arzt für die Behandlung.",
            "pt": "O paciente visita a clínica e conversa com o médico para receber atendimento.",
            "ja": "患者が病院の受付で診察を待つ。",
            "ko": "환자가 병원 접수처에서 진료를 기다립니다.",
            "ru": "Пациент посещает клинику и разговаривает с врачом.",
            "ar": "يزور المريض العيادة ويتحدث مع الطبيب.",
            "hi": "मरीज अस्पताल में डॉक्टर से मिलता है।",
            "id": "Pasien mengunjungi klinik dan berbicara dengan dokter untuk perawatan.",
            "vi": "Bệnh nhân đến phòng khám và nói chuyện với bác sĩ.",
            "it": "Il paziente visita la clinica e parla con il medico per le cure.",
            "tr": "Hasta kliniği ziyaret eder ve doktorla konuşur.",
            "bn": "রোগী ক্লিনিকে চিকিৎসকের সঙ্গে কথা বলেন।",
            "ur": "مریض ہسپتال میں ڈاکٹر سے ملتا ہے۔",
            "fa": "بیمار به درمانگاه می‌رود و با پزشک صحبت می‌کند.",
            "th": "ผู้ป่วยไปที่คลินิกและพูดคุยกับแพทย์",
            "nl": "De patiënt bezoekt de kliniek en spreekt met de arts voor zorg.",
            "pl": "Pacjent odwiedza klinikę i rozmawia z lekarzem o leczeniu.",
            "uk": "Пацієнт відвідує клініку та розмовляє з лікарем.",
            "sw": "Mgonjwa anatembelea kliniki na kuzungumza na daktari.",
        }
        for expected, text in samples.items():
            with self.subTest(language=expected):
                self.assertEqual(detect_language_hint(text)["primary"], expected)
                self.assertIn(expected, SUPPORTED_MAJOR_LANGUAGES)
        self.assertEqual(detect_language_hint("short title")["primary"], "und")
        self.assertEqual(detect_language_hint("short title")["confidence"], "ambiguous")

    def test_language_hints_are_recorded_but_unknown_language_does_not_gate(self):
        source = Presentation()
        for body in ("The patient visits the clinic and meets a doctor for care.",
                     "El paciente visita la clínica y habla con el médico para recibir atención.",
                     "ᓂᑲᒧᐃᐧᐣ unknown script is not rejected"):
            slide = source.slides.add_slide(source.slide_layouts[6])
            textbox = slide.shapes.add_textbox(Inches(.5), Inches(.5), Inches(8), Inches(1))
            textbox.text_frame.text = body
        path = self.root / "multilingual-source.pptx"
        source.save(path)
        pages = semantic_scenes._source_pages(path)
        record = language_support_record(pages)
        self.assertEqual(record["detected_source_languages"], ["en", "es"])
        self.assertEqual([row["primary"] for row in record["source_languages_by_page"]],
                         ["en", "es", "und"])
        self.assertTrue(record["language_hint_is_advisory"])
        self.assertEqual(record["photo_query_language"], "en")

    def test_cross_language_duplicate_scenes_merge_to_one_canonical_photo(self):
        source = Presentation()
        excerpts = ["Patient registration at the outpatient clinic.",
                    "Registro del paciente en la clínica ambulatoria."]
        for body in excerpts:
            slide = source.slides.add_slide(source.slide_layouts[6])
            textbox = slide.shapes.add_textbox(Inches(.5), Inches(.5), Inches(8), Inches(1))
            textbox.text_frame.text = body
        path = self.root / "cross-language-scenes.pptx"
        source.save(path)
        canonical = "patient_registration_outpatient_clinic"
        resolution = {
            "schema_version": "2.2.0",
            "page_coverage": [
                {"page": 1, "scene_keys": ["patient_registration_en"], "no_scene_reason": None,
                 "evidence": [excerpts[0]]},
                {"page": 2, "scene_keys": ["registro_paciente_es"], "no_scene_reason": None,
                 "evidence": [excerpts[1]]},
            ],
            "scenes": [
                {"scene_key": "patient_registration_en", "canonical_scene_id": canonical,
                 "topic": "healthcare", "subject": "patient", "action": "registering",
                 "depiction_type": "illustrative",
                 "illustration_rationale": "Patient registration is a representative way to explain clinic access; it does not claim this source event occurred.",
                 "setting": "outpatient clinic", "label": "Patient registration",
                 "lighting": "soft clean clinic lighting", "composition_camera": "eye-level medium shot",
                 "style": "documentary photography", "quality": "sharp focus, natural detail",
                 "visual_query": "patient checking in at outpatient clinic reception",
                 "page_candidates": [{"page": 1, "relevance": .9, "evidence": [excerpts[0]]}]},
                {"scene_key": "registro_paciente_es", "canonical_scene_id": canonical,
                 "topic": "salud", "subject": "paciente", "action": "registrándose",
                 "depiction_type": "illustrative",
                 "illustration_rationale": "El registro del paciente ilustra el acceso a la clínica; no afirma que el hecho ocurriera en la fuente.",
                 "setting": "clínica ambulatoria", "label": "Registro del paciente",
                 "lighting": "soft clean clinic lighting", "composition_camera": "eye-level medium shot",
                 "style": "documentary photography", "quality": "sharp focus, natural detail",
                 "visual_query": "patient checking in at outpatient clinic reception",
                 "page_candidates": [{"page": 2, "relevance": .9, "evidence": [excerpts[1]]}]},
            ],
        }
        normalized, coverage = semantic_scenes._normalize_semantics(
            resolution, semantic_scenes._source_pages(path), require_canonical_scene_id=True)
        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0]["candidate_pages"], [1, 2])
        self.assertEqual(coverage[0]["scene_keys"], coverage[1]["scene_keys"])
        self.assertEqual(normalized[0]["canonical_scene_id"], canonical)
        self.images["device"]["metadata_override"] = "Patient checking in at outpatient clinic reception"
        provider = FakePhotoProvider(self.images)
        plan = semantic_scenes.build_plan(
            path, semantic_resolution=resolution, provider=provider)
        self.assertEqual(plan["language_support"]["detected_source_languages"], ["en", "es"])
        self.assertEqual(len(plan["images"]), 1)
        self.assertEqual(len(provider.queries), 1)
        self.assertTrue(provider.queries[0].isascii())
        output = self.root / "cross-language-scenes.with-photo.pptx"
        semantic_scenes.apply_plan(path, plan, output)
        plan_path = self.root / "cross-language-scenes.plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        report = semantic_scenes.validate_output(path, output, plan_path)
        self.assertEqual(report["status"], "EXECUTED", report["findings"])

    def test_model_plan_requires_stable_cross_language_scene_identity(self):
        resolution = semantic_fixture()
        resolution["scenes"][0].pop("canonical_scene_id")
        with self.assertRaises(semantic_scenes.SemanticSceneError) as caught:
            semantic_scenes._normalize_semantics(
                resolution, semantic_scenes._source_pages(self.source),
                require_canonical_scene_id=True)
        self.assertEqual(caught.exception.code, "SCENE.CANONICAL_SCENE_ID_MISSING")

    def test_model_scene_requires_explicit_illustrative_disclosure_and_rationale(self):
        pages = semantic_scenes._source_pages(self.source)
        missing_type = semantic_fixture()
        missing_type["scenes"][0].pop("depiction_type")
        with self.assertRaises(semantic_scenes.SemanticSceneError) as caught:
            semantic_scenes._normalize_semantics(
                missing_type, pages, require_canonical_scene_id=True)
        self.assertEqual(caught.exception.code, "SCENE.DEPICTION_TYPE_MISSING")

        false_type = semantic_fixture()
        false_type["scenes"][0]["depiction_type"] = "documentary_evidence"
        with self.assertRaises(semantic_scenes.SemanticSceneError) as caught:
            semantic_scenes._normalize_semantics(
                false_type, pages, require_canonical_scene_id=True)
        self.assertEqual(caught.exception.code, "SCENE.DEPICTION_TYPE_INVALID")

        missing_rationale = semantic_fixture()
        missing_rationale["scenes"][0].pop("illustration_rationale")
        with self.assertRaises(semantic_scenes.SemanticSceneError) as caught:
            semantic_scenes._normalize_semantics(
                missing_rationale, pages, require_canonical_scene_id=True)
        self.assertEqual(caught.exception.code, "SCENE.ILLUSTRATION_RATIONALE_MISSING")

    def test_real_model_plan_fails_closed_on_missing_or_ambiguous_page_coverage(self):
        cases = []

        missing = semantic_fixture()
        missing.pop("page_coverage")
        cases.append(("missing", missing, "SCENE.PAGE_COVERAGE_MISSING"))

        duplicate = semantic_fixture()
        duplicate["page_coverage"].append(dict(duplicate["page_coverage"][0]))
        cases.append(("duplicate", duplicate, "SCENE.PAGE_COVERAGE_DUPLICATED"))

        incomplete = semantic_fixture()
        incomplete["page_coverage"].pop()
        cases.append(("incomplete", incomplete, "SCENE.PAGE_COVERAGE_INCOMPLETE"))

        guessed_page = semantic_fixture()
        guessed_page["page_coverage"][0].pop("page")
        cases.append(("guessed_page", guessed_page, "SCENE.PAGE_COVERAGE_INVALID"))

        remapped_page = semantic_fixture()
        remapped_page["page_coverage"][0]["page"] = 99
        cases.append(("remapped_page", remapped_page, "SCENE.PAGE_COVERAGE_INVALID"))

        generic_no_scene = semantic_fixture()
        row = generic_no_scene["page_coverage"][1]
        row["scene_keys"] = []
        row["no_scene_reason"] = "本页为宏观概念论述或无具体实体场景"
        cases.append(("generic_no_scene", generic_no_scene,
                      "SCENE.PAGE_NO_SCENE_REASON_INVALID"))

        for name, resolution, expected_code in cases:
            with self.subTest(case=name):
                with self.assertRaises(semantic_scenes.SemanticSceneError) as caught:
                    semantic_scenes._normalize_semantics(
                        resolution, semantic_scenes._source_pages(self.source),
                        require_canonical_scene_id=True)
                self.assertEqual(caught.exception.code, expected_code)

    def test_real_model_scene_candidates_are_not_fuzzy_expanded_to_related_pages(self):
        quote1 = "消费者在商超结算台付款。"
        quote2 = "消费的经济学定义包括满足生活需要的行为。"
        pages = [
            {"page": 1, "text": quote1, "text_chars": len(quote1)},
            {"page": 2, "text": quote2, "text_chars": len(quote2)},
        ]
        resolution = {
            "schema_version": "2.2.0",
            "scenes": [{
                "scene_key": "supermarket_checkout_payment",
                "canonical_scene_id": "supermarket_checkout_payment",
            "topic": "消费", "subject": "消费者", "action": "结算付款",
            "depiction_type": "illustrative",
            "illustration_rationale": "商超付款场景能帮助理解消费行为，不表示原文事件真实发生。",
                "setting": "商超结算台", "label": "商超结算",
                "lighting": "bright balanced checkout lighting", "composition_camera": "eye-level medium shot",
                "style": "documentary photography", "quality": "sharp focus, realistic detail",
                "visual_query": "consumer paying supermarket checkout",
                "page_candidates": [{"page": 1, "relevance": .95, "evidence": [quote1]}],
            }],
            "page_coverage": [
                {"page": 1, "scene_keys": ["supermarket_checkout_payment"],
                 "no_scene_reason": None, "evidence": [quote1]},
                {"page": 2, "scene_keys": [],
                 "no_scene_reason": "本页仅解释消费的经济学定义，未出现可由原文支持的具体人物、动作或地点。",
                 "evidence": [quote2]},
            ],
        }
        normalized, coverage = semantic_scenes._normalize_semantics(
            resolution, pages, require_canonical_scene_id=True)
        self.assertEqual(normalized[0]["candidate_pages"], [1])
        self.assertEqual(coverage[0]["scene_keys"], ["supermarket_checkout_payment"])
        self.assertEqual(coverage[1]["scene_keys"], [])

    def test_real_model_plan_does_not_infer_scene_objects_from_coverage_keys(self):
        resolution = semantic_fixture()
        resolution["page_coverage"][0]["scene_keys"] = ["undefined_scene"]
        with self.assertRaises(semantic_scenes.SemanticSceneError) as caught:
            semantic_scenes._normalize_semantics(
                resolution, semantic_scenes._source_pages(self.source),
                require_canonical_scene_id=True)
        self.assertEqual(caught.exception.code, "SCENE.PAGE_SCENE_UNDEFINED")

    def test_machine_policy_covers_user_examples_without_closing_vocabulary(self):
        policy = json.loads(semantic_scenes.POLICY_FILE.read_text(encoding="utf-8"))
        self.assertIn("every PPTX", policy["scope"])
        self.assertIn("open_vocabulary", policy["semantic_scope"])
        self.assertEqual(policy["rules_version"], semantic_scenes.VERSION)
        self.assertEqual(policy["rules_version"], "2.22.0")
        self.assertTrue(policy["rules"][
            "all_empty_scene_plan_reconsideration_requires_first_full_deck_audit_pass"])
        self.assertTrue(policy["rules"][
            "all_empty_scene_plan_reconsideration_uses_same_model_and_original_source_pages"])
        self.assertEqual(policy["rules"]["all_empty_scene_plan_reconsideration_passes_max"], 1)
        self.assertTrue(policy["rules"][
            "empty_scene_reconsideration_with_scenes_requires_third_full_deck_audit"])
        self.assertTrue(policy["rules"][
            "empty_scene_plan_still_blocks_generation_after_reconsideration"])
        examples = "\n".join(policy["behavior"]["examples_are_non_exhaustive"])
        for token in ("消费者", "商超", "发票", "结算台", "生产者", "生产线", "工人",
                      "外卖骑手", "陶瓷杯", "保温杯", "一只手拿杯子喝水", "iPhone", "Mac",
                      "医院", "病人", "挂号", "病床", "医生", "门诊部",
                      "养老院", "公园", "广场舞", "打坐", "冥想",
                      "图书馆", "书籍", "新闻页", "报纸", "上班族", "工地工人",
                      "地铁", "出租车", "飞机", "单车", "双脚", "旅游", "爬山", "景点",
                      "学生", "学校", "课堂"):
            with self.subTest(token=token):
                self.assertIn(token, examples)
        self.assertIn("非穷尽", policy["behavior"]["examples_note"])
        self.assertTrue(policy["rules"]["same_scene_key_or_canonical_subject_action_setting_once_per_deck"])
        self.assertTrue(policy["rules"]["distinct_subscenes_within_one_broad_topic_allowed"])
        self.assertTrue(policy["rules"]["abstract_and_data_topics_are_considered_for_representative_scenes_before_no_scene"])
        self.assertTrue(policy["rules"]["every_stock_scene_photo_is_explicitly_illustrative_not_event_evidence"])
        self.assertTrue(policy["rules"]["illustrative_scene_requires_source_topic_evidence_and_rationale"])
        self.assertTrue(policy["rules"]["illustrative_disclosure_is_bound_to_alt_text_and_slide_notes"])
        self.assertTrue(policy["rules"]["model_page_coverage_must_be_explicit_complete_and_unique"])
        self.assertTrue(policy["rules"]["model_scene_page_candidates_must_be_explicit_and_source_grounded"])
        self.assertTrue(policy["rules"]["independent_second_pass_scene_coverage_audit_required"])
        self.assertTrue(policy["rules"]["coverage_audit_prompt_and_result_must_bind_to_frozen_plan"])
        self.assertTrue(policy["rules"]["unverified_or_injected_coverage_audit_blocks_complete_gate"])
        self.assertTrue(policy["rules"]["no_synthetic_scene_or_page_coverage_repair_for_model_output"])
        self.assertTrue(policy["rules"]["auditor_discovered_gap_repair_requires_complete_source_grounded_scene_fields"])
        self.assertEqual(policy["rules"]["auditor_discovered_gap_repair_anchor_repair_attempts_max"], 1)
        self.assertTrue(policy["rules"]["auditor_discovered_gap_repair_preserves_page_evidence_and_existing_scene_identity"])
        self.assertTrue(policy["rules"]["gap_anchor_repair_prompt_response_result_hashes_required"])
        self.assertTrue(policy["rules"]["gap_anchor_repair_requires_complete_scene_fields_and_third_deck_audit"])
        self.assertTrue(policy["rules"]["gap_anchor_repair_accepts_flat_fields_only_when_nested_terms_absent_and_strictly_valid"])
        self.assertTrue(policy["rules"]["invalid_gap_anchor_error_includes_safe_field_reason"])
        self.assertTrue(policy["rules"]["merged_distinct_scene_replan_requires_source_scoped_delta_and_third_audit"])
        self.assertTrue(policy["rules"]["merged_distinct_scene_replan_preserves_unaffected_page_coverage_and_scene_definitions"])
        self.assertEqual(policy["rules"]["merged_distinct_scene_replan_attempts_per_audited_component_max"], 1)
        self.assertTrue(policy["rules"]["merged_scene_replan_output_contains_only_audited_page_patches"])
        self.assertTrue(policy["rules"]["merge_patch_components_are_bound_to_nonoverlapping_page_and_scene_scopes"])
        self.assertTrue(policy["rules"]["merge_patch_candidate_evidence_is_exact_for_each_target_page"])
        self.assertTrue(policy["rules"]["merge_patch_cannot_duplicate_the_same_concrete_scene"])
        self.assertTrue(policy["rules"]["merge_patch_results_are_persisted_and_replayed_against_the_frozen_full_ledger"])
        self.assertTrue(policy["rules"]["any_gap_repair_requires_third_full_deck_verification"])
        self.assertTrue(policy["rules"]["repaired_plan_delta_must_be_replayed_at_final_gate"])
        self.assertTrue(policy["rules"]["no_fuzzy_scene_candidate_expansion_by_keyword_overlap"])
        self.assertTrue(policy["rules"]["no_scene_requires_specific_grounded_reason"])
        self.assertTrue(policy["rules"]["no_scene_fallback_to_text_card"])
        self.assertTrue(policy["rules"]["major_multilingual_inputs_supported"])
        self.assertTrue(policy["rules"]["language_hint_is_advisory_not_a_rejection_gate"])
        self.assertTrue(policy["rules"]["detect_language_before_any_scene_keyword_extraction"])
        self.assertEqual(policy["rules"]["legacy_keyword_library_language_scope"],
                         "zh_only_after_page_language_detection")
        self.assertTrue(policy["rules"]["non_chinese_unknown_or_mixed_pages_bypass_legacy_chinese_keyword_matching"])
        self.assertTrue(policy["rules"]["non_chinese_pages_continue_to_multilingual_open_vocabulary_scene_planner"])
        self.assertTrue(policy["rules"]["scene_planner_receives_original_text_and_detected_language_context"])
        self.assertTrue(policy["rules"]["invalid_initial_photo_search_anchors_get_one_same_model_scoped_repair"])
        self.assertTrue(policy["rules"]["photo_anchor_repair_cannot_change_scene_identity_page_evidence_or_coverage"])
        self.assertTrue(policy["rules"]["photo_anchor_repair_must_pass_the_unchanged_strict_anchor_validator"])
        self.assertTrue(policy["rules"]["photo_anchor_repair_prompt_response_and_result_hashes_required"])
        self.assertTrue(policy["rules"]["validated_photo_anchors_are_concatenated_without_dropping_or_reordering_tokens"])
        self.assertTrue(policy["rules"]["cross_language_same_scene_uses_one_canonical_scene_id"])
        self.assertIn("ja", policy["behavior"]["multilingual_input_languages"])
        self.assertIn("ar", policy["behavior"]["multilingual_input_languages"])
        self.assertTrue(policy["rules"]["empty_area_photo_preserves_full_source_aspect"])
        self.assertIn("fully contain photos in empty areas without cropping",
                      policy["behavior"]["photo_framing"])

    def test_model_runtime_error_is_not_misclassified_as_quota(self):
        missing_binary = classify_external_model_failure(RuntimeError(
            "SceneModelError: error starting llama-server: llama-server binary not found "
            "(checked: /Users/example/Library/Application Support/LocalQwenService/bin/llama-server)"))
        self.assertIsNotNone(missing_binary)
        self.assertEqual(missing_binary.code, "MODEL.RUNTIME_UNAVAILABLE")
        self.assertNotIn("/Users/", missing_binary.detail)
        quota = classify_external_model_failure(RuntimeError(
            "HTTP 402 insufficient credits for model invocation"))
        self.assertIsNotNone(quota)
        self.assertEqual(quota.code, "MODEL.QUOTA_EXCEEDED")
        unrelated = classify_external_model_failure(RuntimeError("layout file not found"))
        self.assertIsNone(unrelated)

    def test_repeated_scene_is_placed_once_but_distinct_scene_gets_own_photo(self):
        provider = FakePhotoProvider(self.images)
        plan = semantic_scenes.build_plan(self.source, provider=provider,
                                          semantic_resolution=semantic_fixture())
        self.assertEqual(plan["unresolved_scenes"], [])
        self.assertEqual(set(plan["images"]),
                         {"hand_drinking_from_ceramic_cup", "iphone_mac_work_use"})
        self.assertEqual(len(provider.queries), 2)
        self.assertEqual(plan["images"]["hand_drinking_from_ceramic_cup"]["page"], 1)
        self.assertEqual(plan["images"]["iphone_mac_work_use"]["page"], 2)
        self.assertNotEqual(plan["images"]["hand_drinking_from_ceramic_cup"]["sha256"],
                            plan["images"]["iphone_mac_work_use"]["sha256"])
        output = self.root / "candidate.pptx"
        semantic_scenes.apply_plan(self.source, plan, output)
        plan_path = self.root / "scene_plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        report = semantic_scenes.validate_output(self.source, output, plan_path)
        self.assertEqual(report["status"], "EXECUTED", report["findings"])
        self.assertEqual(report["summary"]["embedded_scene_picture_count"], 2)
        rendered = Presentation(str(output))
        for slide in rendered.slides:
            for shape in slide.shapes:
                if not shape.name.startswith("WENGAO_SCENE|"):
                    continue
                scene_key = shape.name.split("|", 2)[1]
                spec = plan["images"][scene_key]
                original_aspect = spec["actual_width"] / spec["actual_height"]
                displayed_aspect = shape.width / shape.height
                self.assertAlmostEqual(displayed_aspect, original_aspect, delta=0.02)
                self.assertEqual((shape.crop_left, shape.crop_top,
                                  shape.crop_right, shape.crop_bottom), (0, 0, 0, 0))

    def _scene_render_fixture(self, *, occlude_fraction: float = 0.0):
        for offset, (key, title) in enumerate((
                ("cup", "Ceramic cup drinking"),
                ("device", "iPhone Mac work desk")), 1):
            noise = Image.effect_noise((800, 600), 100).convert("L")
            channels = [noise.point(lambda value, shift=offset * factor:
                                    (value + shift) % 256)
                        for factor in (19, 53, 97)]
            textured = Image.merge("RGB", channels)
            path = self.assets / f"pixel-texture-{key}.png"
            textured.save(path)
            self.images[key] = {"file": str(path), "title": title}
        provider = FakePhotoProvider(self.images)
        plan = semantic_scenes.build_plan(self.source, provider=provider,
                                          semantic_resolution=semantic_fixture())
        candidate = self.root / f"pixel-candidate-{occlude_fraction}.pptx"
        semantic_scenes.apply_plan(self.source, plan, candidate)
        plan_path = self.root / f"pixel-plan-{occlude_fraction}.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        evidence = synthetic_scene_render_evidence(
            candidate, plan, self.root / f"pixel-pages-{occlude_fraction}",
            occlude_fraction=occlude_fraction)
        return candidate, plan_path, plan, evidence

    def test_complete_scene_gate_requires_bound_full_page_pixel_evidence(self):
        candidate, plan_path, _plan, evidence = self._scene_render_fixture()
        report = semantic_scenes.validate_complete_output(
            self.source, candidate, plan_path, evidence,
            require_visual_review=False)
        self.assertEqual(report["status"], "NOT_EVALUABLE", report)
        self.assertTrue(report["blocking"])
        self.assertEqual(report["summary"]["rendered_pixel_visibility"]["rendered_scene_count"], 2)
        self.assertEqual(report["summary"]["coverage_audit_status"], "NOT_EVALUABLE")
        self.assertIn("SCENE.COVERAGE_AUDIT_UNVERIFIED",
                      {item["code"] for item in report["unverified"]})

        missing = semantic_scenes.validate_complete_output(
            self.source, candidate, plan_path, None,
            require_visual_review=False)
        self.assertEqual(missing["status"], "NOT_EVALUABLE")
        self.assertTrue(missing["blocking"])
        self.assertIn("SCENE.RENDER_EVIDENCE_MISSING",
                      {item["code"] for item in missing["unverified"]})

    def test_local_visual_review_binds_real_image_and_scene_and_blocks_uncertainty(self):
        from wengao_ppt import scene_model

        image_bytes = png_bytes(4)
        scene = {"scene_key": "classroom_reading", "subject": "students reading",
                 "action": "reading books", "setting": "classroom"}
        accepted = {
            "scene_match": True, "subject_visible": True,
            "action_visible": True, "setting_visible": True,
            "photographic": True, "confidence": "high", "reason_code": "NONE",
        }
        raw_text = json.dumps(accepted)
        with patch("wengao_ppt.scene_model.call_local_vision_model", return_value={
                "response": raw_text, "model": "fixture-qwen-vision",
                "response_sha256": hashlib.sha256(raw_text.encode()).hexdigest(),
                "elapsed_seconds": .01}):
            proof = scene_model.review_semantic_scene_image(
                "fixture-qwen-vision", scene, image_bytes)
        self.assertEqual(proof["status"], "PASSED")
        self.assertEqual(proof["image_sha256"], hashlib.sha256(image_bytes).hexdigest())
        self.assertEqual(proof["scene_identity_sha256"],
                         scene_visual_identity_sha256(scene))

        uncertain = dict(accepted, action_visible=False, confidence="medium",
                         reason_code="ACTION_NOT_VISIBLE")
        raw_text = json.dumps(uncertain)
        with patch("wengao_ppt.scene_model.call_local_vision_model", return_value={
                "response": raw_text, "model": "fixture-qwen-vision",
                "response_sha256": hashlib.sha256(raw_text.encode()).hexdigest(),
                "elapsed_seconds": .01}):
            blocked = scene_model.review_semantic_scene_image(
                "fixture-qwen-vision", scene, image_bytes)
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertFalse(blocked["action_visible"])

    def test_production_scene_gate_blocks_missing_or_tampered_visual_review(self):
        provider = FakePhotoProvider(self.images)
        plan = semantic_scenes.build_plan(
            self.source, provider=provider, semantic_resolution=semantic_fixture())
        candidate = self.root / "visual-review-bound-candidate.pptx"
        semantic_scenes.apply_plan(self.source, plan, candidate)
        plan_path = self.root / "visual-review-bound-plan.json"

        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        missing = semantic_scenes.validate_output(
            self.source, candidate, plan_path, require_visual_review=True)
        self.assertIn("SCENE.VISUAL_REVIEW_UNVERIFIED",
                      {item["code"] for item in missing["findings"]})

        scenes = {row["scene_key"]: row for row in plan["scenes"]}
        for key, spec in plan["images"].items():
            image_bytes = Path(spec["file"]).read_bytes()
            spec["visual_review"] = fixture_visual_review_call(
                "fixture-local-vision", scenes[key], image_bytes)
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        accepted = semantic_scenes.validate_output(
            self.source, candidate, plan_path, require_visual_review=True)
        accepted_codes = {item["code"] for item in accepted["findings"]}
        self.assertNotIn("SCENE.VISUAL_REVIEW_UNVERIFIED", accepted_codes)

        first_key = next(iter(plan["images"]))
        plan["images"][first_key]["visual_review"]["image_sha256"] = "0" * 64
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        tampered = semantic_scenes.validate_output(
            self.source, candidate, plan_path, require_visual_review=True)
        self.assertIn("SCENE.VISUAL_REVIEW_UNVERIFIED",
                      {item["code"] for item in tampered["findings"]})

    def test_rendered_scene_pixel_gate_fails_when_photo_is_hidden_by_opaque_layer(self):
        candidate, plan_path, _plan, evidence = self._scene_render_fixture(occlude_fraction=1.0)
        report = semantic_scenes.validate_rendered_scene_visibility(
            candidate, plan_path, evidence)
        self.assertEqual(report["status"], "FAILED", report)
        self.assertTrue(report["blocking"])
        self.assertIn("SCENE.RENDERED_IMAGE_NOT_VISIBLE",
                      {item["code"] for item in report["findings"]})

    def test_rendered_scene_pixel_gate_fails_when_half_the_photo_is_occluded(self):
        candidate, plan_path, _plan, evidence = self._scene_render_fixture(occlude_fraction=0.5)
        report = semantic_scenes.validate_rendered_scene_visibility(
            candidate, plan_path, evidence)
        self.assertEqual(report["status"], "FAILED", report)
        self.assertTrue(report["blocking"])
        self.assertIn("SCENE.RENDERED_IMAGE_NOT_VISIBLE",
                      {item["code"] for item in report["findings"]})

    def test_rendered_scene_pixel_gate_blocks_antigravity_extreme_cover_crop(self):
        candidate, plan_path, plan, _evidence = self._scene_render_fixture()
        cropped = self.root / "antigravity-extreme-cover-crop.pptx"
        prs = Presentation(str(candidate))
        marked = [shape for slide in prs.slides for shape in slide.shapes
                  if shape.name.startswith("WENGAO_SCENE|")]
        self.assertTrue(marked)
        marked[0].crop_top = 0.46223
        marked[0].crop_bottom = 0.46223
        prs.save(cropped)
        evidence = synthetic_scene_render_evidence(
            cropped, plan, self.root / "antigravity-extreme-cover-crop-pages")

        report = semantic_scenes.validate_rendered_scene_visibility(
            cropped, plan_path, evidence)
        self.assertEqual(report["status"], "FAILED", report)
        self.assertTrue(report["blocking"])
        finding = next(item for item in report["findings"]
                       if item["code"] == "SCENE.RENDER_SOURCE_CONTENT_CROPPED")
        self.assertAlmostEqual(finding["source_retained_fraction"],
                               (1 - 0.46223 * 2), places=4)

    def test_rendered_scene_pixel_gate_hashes_both_real_render_pdf_artifacts(self):
        candidate, plan_path, _plan, evidence = self._scene_render_fixture()
        pdf_path = Path(evidence["suppressed_rendered_pdf_path"])
        pdf_path.write_bytes(pdf_path.read_bytes() + b"tampered")
        report = semantic_scenes.validate_rendered_scene_visibility(
            candidate, plan_path, evidence)
        self.assertEqual(report["status"], "FAILED", report)
        self.assertTrue(report["blocking"])
        self.assertIn("SCENE.RENDER_SUPPRESSED_PDF_HASH_MISMATCH",
                      {item["code"] for item in report["findings"]})

    def test_rendered_scene_pixel_gate_rejects_incomplete_full_page_pair(self):
        candidate, plan_path, _plan, evidence = self._scene_render_fixture()
        evidence["pages"] = evidence["pages"][:-1]
        report = semantic_scenes.validate_rendered_scene_visibility(
            candidate, plan_path, evidence)
        self.assertEqual(report["status"], "NOT_EVALUABLE", report)
        self.assertTrue(report["blocking"])
        self.assertIn("SCENE.RENDER_PAGE_SCOPE_INCOMPLETE",
                      {item["code"] for item in report["unverified"]})

    def test_scene_suppressed_reference_only_removes_marked_scene_pictures(self):
        candidate, _plan_path, plan, _evidence = self._scene_render_fixture()
        reference = self.root / "reference-without-scene-pictures.pptx"
        identity = semantic_scenes.make_scene_suppressed_pptx(candidate, reference)
        self.assertEqual(identity["removed_count"], len(plan["images"]))
        self.assertEqual({row["scene_key"] for row in identity["removed_scene_pictures"]},
                         set(plan["images"]))
        source_members = {}
        reference_members = {}
        with __import__("zipfile").ZipFile(candidate) as archive:
            source_members = {name: archive.read(name) for name in archive.namelist()}
        with __import__("zipfile").ZipFile(reference) as archive:
            reference_members = {name: archive.read(name) for name in archive.namelist()}
        self.assertEqual(set(reference_members), set(source_members))
        for name in source_members:
            if not name.startswith("ppt/slides/slide") or not name.endswith(".xml"):
                self.assertEqual(reference_members[name], source_members[name], name)

    def test_rendered_scene_pixel_gate_rehashes_every_page_png(self):
        candidate, plan_path, _plan, evidence = self._scene_render_fixture()
        page_path = Path(evidence["pages"][0]["png_path"])
        page_path.write_bytes(page_path.read_bytes() + b"tampered")
        report = semantic_scenes.validate_rendered_scene_visibility(
            candidate, plan_path, evidence)
        self.assertEqual(report["status"], "FAILED", report)
        self.assertIn("SCENE.RENDER_PAGE_HASH_MISMATCH",
                      {item["code"] for item in report["findings"]})

    def test_portrait_and_landscape_photos_keep_full_aspect_in_safe_areas(self):
        portrait_path = self.assets / "portrait-cup.png"
        portrait = Image.new("RGB", (600, 1000), (95, 48, 32))
        ImageDraw.Draw(portrait).ellipse((140, 170, 460, 490), fill=(235, 204, 120))
        portrait.save(portrait_path)
        self.images["cup"] = {"file": str(portrait_path), "title": "Ceramic cup drinking"}
        source = make_compact_source_pptx(self.root / "compact-source.pptx")
        plan = semantic_scenes.build_plan(source, provider=FakePhotoProvider(self.images),
                                          semantic_resolution=semantic_fixture())
        self.assertEqual(plan["unresolved_scenes"], [])
        self.assertEqual(plan["images"]["hand_drinking_from_ceramic_cup"]["actual_width"], 600)
        self.assertEqual(plan["images"]["hand_drinking_from_ceramic_cup"]["actual_height"], 1000)
        output = self.root / "portrait-landscape-candidate.pptx"
        semantic_scenes.apply_plan(source, plan, output)
        plan_path = self.root / "portrait-landscape-plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        report = semantic_scenes.validate_output(source, output, plan_path)
        self.assertEqual(report["status"], "EXECUTED", report["findings"])
        rendered = Presentation(str(output))
        for slide in rendered.slides:
            for shape in slide.shapes:
                if not shape.name.startswith("WENGAO_SCENE|"):
                    continue
                scene_key = shape.name.split("|", 2)[1]
                spec = plan["images"][scene_key]
                self.assertAlmostEqual(shape.width / shape.height,
                                       spec["actual_width"] / spec["actual_height"],
                                       delta=0.02)
                self.assertEqual((shape.crop_left, shape.crop_top,
                                  shape.crop_right, shape.crop_bottom), (0, 0, 0, 0))

    def test_extreme_replacement_slot_crop_is_not_planned_or_accepted(self):
        placeholder_path = self.assets / "generic-placeholder.png"
        placeholder_path.write_bytes(png_bytes(7))
        source_with_slot = self.root / "source-with-wide-visual-slot.pptx"
        prs = Presentation(str(self.source))
        slot_rect = [7.0, 0.5, 5.8, 2.2]
        prs.slides[0].shapes.add_picture(
            str(placeholder_path), Inches(slot_rect[0]), Inches(slot_rect[1]),
            Inches(slot_rect[2]), Inches(slot_rect[3]))
        prs.save(source_with_slot)
        placeholder_sha = hashlib.sha256(placeholder_path.read_bytes()).hexdigest()
        visual_plan = {"images": {"1": {"page": 1, "rect": slot_rect,
                                         "sha256": placeholder_sha,
                                         "file": str(placeholder_path)}}}

        plan = semantic_scenes.build_plan(
            source_with_slot, visual_plan=visual_plan,
            provider=FakePhotoProvider(self.images),
            semantic_resolution=semantic_fixture())
        cup = plan["images"]["hand_drinking_from_ceramic_cup"]
        self.assertFalse(cup["placement_mode"] == "replace_visual_image")
        self.assertEqual(cup["fit_mode"], "contain")

        # Recreate the unsafe slot result as a tampered/stale plan. Validation
        # must reject the generated OOXML instead of trusting a valid plan hash.
        cup.update({"page": 1, "rect": slot_rect, "target_rect": slot_rect,
                    "placement_mode": "replace_visual_image", "fit_mode": "cover",
                    "replaced_visual_sha256": placeholder_sha})
        plan["plan_sha256"] = semantic_scenes._plan_hash(plan)
        candidate = self.root / "extreme-cover-candidate.pptx"
        semantic_scenes.apply_plan(source_with_slot, plan, candidate)
        plan_path = self.root / "extreme-cover-plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        report = semantic_scenes.validate_output(source_with_slot, candidate, plan_path)
        codes = {item["code"] for item in report["findings"]}
        self.assertEqual(report["status"], "FAILED")
        self.assertIn("SCENE.PHOTO_CROP_EXCESSIVE", codes)

    def test_replacement_slot_cover_within_policy_keeps_expected_crop_geometry(self):
        placeholder_path = self.assets / "safe-generic-placeholder.png"
        placeholder_path.write_bytes(png_bytes(8))
        source_with_slot = self.root / "source-with-safe-visual-slot.pptx"
        prs = Presentation(str(self.source))
        slot_rect = [7.0, 0.5, 5.8, 3.8]
        prs.slides[0].shapes.add_picture(
            str(placeholder_path), Inches(slot_rect[0]), Inches(slot_rect[1]),
            Inches(slot_rect[2]), Inches(slot_rect[3]))
        prs.save(source_with_slot)
        placeholder_sha = hashlib.sha256(placeholder_path.read_bytes()).hexdigest()
        visual_plan = {"images": {"1": {"page": 1, "rect": slot_rect,
                                         "sha256": placeholder_sha,
                                         "file": str(placeholder_path)}}}
        plan = semantic_scenes.build_plan(
            source_with_slot, visual_plan=visual_plan,
            provider=FakePhotoProvider(self.images),
            semantic_resolution=semantic_fixture())
        cup = plan["images"]["hand_drinking_from_ceramic_cup"]
        self.assertEqual(cup["placement_mode"], "replace_visual_image")
        self.assertEqual(cup["fit_mode"], "cover")
        candidate = self.root / "safe-cover-candidate.pptx"
        semantic_scenes.apply_plan(source_with_slot, plan, candidate)
        plan_path = self.root / "safe-cover-plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        report = semantic_scenes.validate_output(source_with_slot, candidate, plan_path)
        self.assertEqual(report["status"], "EXECUTED", report["findings"])

    def test_alternative_query_is_used_only_after_primary_has_no_match(self):
        resolution = semantic_fixture()
        primary = "ceramic cup drinking water kitchen"
        alternative = "hand drinking water from ceramic mug kitchen"
        resolution["scenes"][0]["visual_query_alternatives"] = [alternative]
        provider = FakePhotoProvider(self.images, fail_queries={primary})
        plan = semantic_scenes.build_plan(self.source, provider=provider,
                                          semantic_resolution=resolution)
        self.assertFalse(plan["unresolved_scenes"])
        self.assertEqual(provider.queries,
                         ["iphone mac work communication desk", primary, alternative])
        self.assertEqual(plan["images"]["hand_drinking_from_ceramic_cup"]["visual_query"],
                         alternative)

    def test_synonym_search_reformulation_keeps_original_scene_gate(self):
        anchors = {"subject": "employee", "action": "recording completion status",
                   "setting": "office"}
        canonical = "employee recording completion status office"
        unrelated = {
            "provider": "fixture", "source_key": "music-studio-recording",
            "title": "Employee recording a music album in a studio",
            "description": "A musician records an album at a recording studio.",
            "download_url": "https://images.example.org/music.png",
            "license": "CC0 1.0", "width": 1600, "height": 900,
        }

        class Search:
            name = "fixture"
            allowed_hosts = {"images.example.org"}

            def search(self, provider_query):
                return [unrelated]

        with tempfile.TemporaryDirectory() as cache:
            provider = ScenePhotoProvider(cache, providers=[Search()])
            context = {"photo_search_terms": anchors, "metadata_query": canonical,
                       "style": "documentary photography"}
            with patch("wengao_ppt.scene_images._download_image") as download:
                with self.assertRaises(SceneImageError):
                    provider.fetch_one("musician recording album studio",
                                       scene_context=context)
        download.assert_not_called()
        self.assertTrue(any(row.get("code") == "SCENE.IMAGE_CONTEXT_UNVERIFIED"
                            for row in provider.errors))

    def test_gate_rejects_duplicate_canonical_scene_with_different_keys(self):
        provider = FakePhotoProvider(self.images)
        plan = semantic_scenes.build_plan(self.source, provider=provider,
                                          semantic_resolution=semantic_fixture())
        output = self.root / "duplicate-identity.pptx"
        semantic_scenes.apply_plan(self.source, plan, output)
        duplicated = next(scene for scene in plan["scenes"]
                          if scene["scene_key"] == "iphone_mac_work_use")
        original = next(scene for scene in plan["scenes"]
                        if scene["scene_key"] == "hand_drinking_from_ceramic_cup")
        for field in ("subject", "action", "setting"):
            duplicated[field] = original[field]
        plan["plan_sha256"] = semantic_scenes._plan_hash(plan)
        plan_path = self.root / "duplicate-identity-plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        report = semantic_scenes.validate_output(self.source, output, plan_path)
        self.assertEqual(report["status"], "FAILED")
        self.assertIn("SCENE.DUPLICATE_SCENE_IDENTITY",
                      {row["code"] for row in report["findings"]})

    def test_photo_reuse_across_distinct_scenes_blocks(self):
        provider = FakePhotoProvider(self.images, reuse_first=True)
        plan = semantic_scenes.build_plan(self.source, provider=provider,
                                          semantic_resolution=semantic_fixture())
        self.assertTrue(plan["unresolved_scenes"])
        self.assertTrue(any(row["reason"] == "SCENE.PHOTO_REUSED_OR_UNVERIFIED"
                            for row in plan["unresolved_scenes"]))

    def test_reencoded_near_duplicate_photo_blocks(self):
        original = png_bytes(2)
        reencoded = png_bytes(2, metadata="different container bytes")
        self.assertNotEqual(hashlib.sha256(original).hexdigest(), hashlib.sha256(reencoded).hexdigest())
        self.assertLessEqual(image_hash_distance(perceptual_hash(original),
                                                 perceptual_hash(reencoded)), 4)
        self.images["cup"]["file"] = str(self.assets / "same-a.png")
        self.images["device"]["file"] = str(self.assets / "same-b.png")
        Path(self.images["cup"]["file"]).write_bytes(original)
        Path(self.images["device"]["file"]).write_bytes(reencoded)
        provider = FakePhotoProvider(self.images)
        plan = semantic_scenes.build_plan(self.source, provider=provider,
                                          semantic_resolution=semantic_fixture())
        self.assertTrue(any(row["reason"] == "SCENE.NEAR_DUPLICATE_PHOTO"
                            for row in plan["unresolved_scenes"]))

    def test_distinct_subscenes_under_one_topic_remain_distinct(self):
        source = Presentation()
        slide = source.slides.add_slide(source.slide_layouts[6])
        quote = "阅读包括去图书馆借书、在家读报和查看新闻页面。"
        box = slide.shapes.add_textbox(Inches(.5), Inches(.5), Inches(8), Inches(1.5))
        box.text_frame.text = quote
        path = self.root / "reading.pptx"
        source.save(path)
        descriptions = [
            ("reading_in_library", "人在图书馆借书", "借阅书籍", "图书馆", "library visitor borrowing books", "去图书馆借书"),
            ("reading_newspaper_at_home", "人在家阅读报纸", "读报", "家庭", "person reading newspaper at home", "在家读报"),
            ("reading_news_page", "读者查看新闻页面", "浏览新闻", "屏幕", "reader viewing a news page on screen", "查看新闻页面"),
        ]
        scenes = []
        keys = []
        for key, subject, action, setting, query, evidence in descriptions:
            keys.append(key)
            scenes.append({"scene_key": key, "canonical_scene_id": key,
                           "topic": "阅读", "subject": subject,
                           "action": action, "setting": setting, "label": subject,
                           "visual_query": query,
                           "page_candidates": [{"page": 1, "relevance": .8,
                                                "evidence": [evidence]}]})
        resolution = {"page_coverage": [{"page": 1, "scene_keys": keys,
                                        "no_scene_reason": None, "evidence": [quote]}],
                      "scenes": scenes}
        normalized, _ = semantic_scenes._normalize_semantics(
            resolution, semantic_scenes._source_pages(path))
        self.assertEqual({row["scene_key"] for row in normalized}, set(keys))

    def test_open_vocabulary_accepts_a_scene_outside_all_user_examples(self):
        source = Presentation()
        slide = source.slides.add_slide(source.slide_layouts[6])
        quote = "检修人员在海上风电机组顶部更换叶片传感器。"
        box = slide.shapes.add_textbox(Inches(.5), Inches(.5), Inches(8), Inches(1.5))
        box.text_frame.text = quote
        path = self.root / "offshore-wind-maintenance.pptx"
        source.save(path)
        arbitrary_scene = {
            "page_coverage": [{"page": 1,
                               "scene_keys": ["offshore_wind_turbine_sensor_maintenance"],
                               "no_scene_reason": None, "evidence": [quote]}],
            "scenes": [{"scene_key": "offshore_wind_turbine_sensor_maintenance",
                        "topic": "海上风电运维", "subject": "风机检修人员",
                        "action": "更换叶片传感器", "setting": "海上风电机组顶部",
                        "label": "海上风电机组检修",
                        "visual_query": "technician replacing wind turbine blade sensor offshore",
                        "page_candidates": [{"page": 1, "relevance": .9,
                                             "evidence": [quote]}]}],
        }
        pages = semantic_scenes._source_pages(path)
        normalized, coverage = semantic_scenes._normalize_semantics(arbitrary_scene, pages)
        self.assertEqual(normalized[0]["scene_key"],
                         "offshore_wind_turbine_sensor_maintenance")
        self.assertEqual(coverage[0]["scene_keys"],
                         ["offshore_wind_turbine_sensor_maintenance"])

    def test_no_scene_or_missing_photo_never_falls_back_to_text(self):
        quote = "本页没有可具体化的场景，仅有抽象定义。"
        source = Presentation()
        slide = source.slides.add_slide(source.slide_layouts[6])
        box = slide.shapes.add_textbox(Inches(.5), Inches(.5), Inches(7), Inches(1))
        box.text_frame.text = quote
        path = self.root / "abstract.pptx"
        source.save(path)
        resolution = {"page_coverage": [{"page": 1, "scene_keys": [],
                                        "no_scene_reason": "只有抽象定义",
                                        "evidence": [quote]}], "scenes": []}
        plan = semantic_scenes.build_plan(path, semantic_resolution=resolution,
                                          provider=FakePhotoProvider(self.images))
        self.assertTrue(plan["unresolved_scenes"])
        self.assertEqual(plan["unresolved_scenes"][0]["reason"], "SCENE.NO_SCENES_PLANNED")
        with self.assertRaises(semantic_scenes.SemanticSceneError):
            semantic_scenes.apply_plan(path, plan, self.root / "must-not-exist.pptx")
        self.assertFalse((self.root / "must-not-exist.pptx").exists())

    def test_full_slide_card_blocks_photo_placement(self):
        # A full-slide (or full-height half-slide) opaque color fill sits
        # behind everything in z-order — it is always among the first shapes
        # in document order, and a newly inserted photo is appended later and
        # drawn on top of it. Treating it as a blocking "card" made scene
        # photo placement impossible on every page of any themed deck that
        # uses this extremely common background-panel pattern (2026-10-27
        # job-c915c8c7deff: 0 of 7 planned, source-grounded scenes could be
        # placed anywhere in a 13-page deck because every page's backdrop
        # panel alone was enough to reject every candidate rect).
        source = make_source_pptx(self.root / "blocked.pptx", full_slide_fill=True)
        resolution = semantic_fixture()
        provider = FakePhotoProvider(self.images)
        plan = semantic_scenes.build_plan(source, semantic_resolution=resolution, provider=provider)
        self.assertEqual(plan["unplaced_scenes"], [])
        self.assertEqual(plan["unresolved_scenes"], [])
        self.assertEqual(set(plan["images"]),
                         {"hand_drinking_from_ceramic_cup", "iphone_mac_work_use"})

    def test_query_privacy_guard_rejects_numbers_and_source_text(self):
        pages = semantic_scenes._source_pages(self.source)
        scene = {"scene_key": "private", "visual_query": "private metric 47 percent at work",
                 "page_candidates": [{"evidence": ["一只手拿着陶瓷杯喝水的场景"]}]}
        with self.assertRaises(semantic_scenes.SemanticSceneError):
            semantic_scenes._privacy_query_check(scene, pages)
        scene["visual_query"] = "a unique phrase copied from a private source excerpt"
        scene["page_candidates"] = [{"evidence": ["a unique phrase copied from a private source excerpt"]}]
        with self.assertRaises(semantic_scenes.SemanticSceneError):
            semantic_scenes._privacy_query_check(scene, pages)

    def test_image_source_url_rejects_private_hosts(self):
        from wengao_ppt.scene_images import _url_is_public_https
        self.assertFalse(_url_is_public_https("https://127.0.0.1/image.png"))
        self.assertFalse(_url_is_public_https("https://localhost/image.png"))
        self.assertFalse(_url_is_public_https("http://images.example.org/image.png"))

    def test_official_commons_http_license_deed_is_allowed_but_other_http_is_not(self):
        self.assertTrue(is_valid_license_url(
            "http://creativecommons.org/publicdomain/zero/1.0/deed.en", "CC0 1.0"))
        self.assertTrue(is_valid_license_url(
            "http://creativecommons.org/publicdomain/mark/1.0/", "PDM 1.0"))
        self.assertFalse(is_valid_license_url(
            "http://creativecommons.org/licenses/by/4.0/", "CC0 1.0"))
        self.assertFalse(is_valid_license_url(
            "https://creativecommons.org/licenses/by-nc/4.0/", "CC0 1.0"))
        self.assertFalse(is_allowed_license(
            "Public Domain", license_url="https://creativecommons.org/licenses/by-sa/4.0/"))
        self.assertFalse(is_valid_license_url(
            "http://creativecommons.org.evil.example/publicdomain/zero/1.0/", "CC0 1.0"))
        self.assertFalse(is_valid_license_url(
            "http://images.example.org/cc0", "CC0 1.0"))

    def test_only_canonical_cc_by_4_0_is_allowed_and_requires_attribution_link(self):
        by4 = "https://creativecommons.org/licenses/by/4.0/"
        self.assertTrue(is_allowed_license("by", version="4.0", license_url=by4))
        self.assertTrue(is_allowed_license("CC BY 4.0", license_url=by4))
        self.assertFalse(is_allowed_license("by", version="3.0", license_url=by4))
        self.assertFalse(is_allowed_license("by", version="4.0"))
        self.assertFalse(is_allowed_license(
            "CC BY-SA 4.0", version="4.0",
            license_url="https://creativecommons.org/licenses/by-sa/4.0/"))
        self.assertFalse(is_allowed_license(
            "CC BY-NC 4.0", version="4.0",
            license_url="https://creativecommons.org/licenses/by-nc/4.0/"))
        self.assertFalse(is_allowed_license(
            "CC BY 4.0", license_url="https://example.org/licenses/by/4.0/"))
        self.assertTrue(is_valid_license_url(by4, "CC BY 4.0"))
        self.assertFalse(is_valid_license_url(
            "https://example.org/licenses/by/4.0/", "CC BY 4.0"))

    def test_openverse_requests_commercial_cc_by_results_and_filters_version(self):
        good = {
            "id": "good-by4", "title": "Factory worker assembling parts",
            "creator": "A. Photographer", "license": "by", "license_version": "4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "thumbnail": "https://images.example.org/good.jpg",
            "foreign_landing_url": "https://source.example.org/good",
            "width": 1600, "height": 900,
        }
        wrong_version = {**good, "id": "old-by", "license_version": "3.0",
                         "license_url": "https://creativecommons.org/licenses/by/3.0/"}
        share_alike = {**good, "id": "by-sa", "license": "by-sa",
                       "license_url": "https://creativecommons.org/licenses/by-sa/4.0/"}
        with patch("wengao_ppt.scene_images._request_json",
                   return_value={"results": [wrong_version, share_alike, good]}) as request, \
                patch("wengao_ppt.scene_images._url_is_public_https", return_value=True):
            rows = OpenversePhotoProvider().search("factory worker assembly", max_attempts=1)
        self.assertEqual([row["source_key"] for row in rows],
                         ["old-by", "by-sa", "good-by4"])
        allowed = [row for row in rows if is_allowed_license(
            row.get("license", ""), version=row.get("license_version"),
            license_url=row.get("license_url"))]
        self.assertEqual([row["source_key"] for row in allowed], ["good-by4"])
        self.assertEqual(allowed[0]["license"], "CC BY 4.0")
        query = __import__("urllib.parse").parse.parse_qs(
            __import__("urllib.parse").parse.urlparse(request.call_args.args[0]).query)
        self.assertEqual(query["license"], ["cc0,pdm,by"])
        self.assertEqual(query["license_type"], ["commercial"])

    def test_photo_attribution_is_appended_to_notes_and_bound_by_output_gate(self):
        source = self.root / "speaker-notes-source.pptx"
        make_source_pptx(source)
        prs = Presentation(str(source))
        for slide in prs.slides:
            slide.notes_slide.notes_text_frame.text = "Original speaker note stays."
        prs.save(source)
        plan = semantic_scenes.build_plan(
            source, provider=FakePhotoProvider(self.images), semantic_resolution=semantic_fixture())
        for spec in plan["images"].values():
            spec["license"] = "CC BY 4.0"
            spec["license_version"] = "4.0"
            spec["license_url"] = "https://creativecommons.org/licenses/by/4.0/"
        plan["plan_sha256"] = semantic_scenes._plan_hash(plan)
        candidate = self.root / "speaker-notes-candidate.pptx"
        semantic_scenes.apply_plan(source, plan, candidate)
        plan_path = self.root / "speaker-notes-plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        report = semantic_scenes.validate_output(source, candidate, plan_path)
        self.assertEqual(report["status"], "EXECUTED", report)
        rendered = Presentation(str(candidate))
        for slide in rendered.slides:
            notes = slide.notes_slide.notes_text_frame.text
            self.assertIn("Original speaker note stays.", notes)
            self.assertIn("[WENGAO PHOTO CREDIT]", notes)
            self.assertIn("Depiction: Illustrative scene", notes)
            self.assertIn("Illustration basis:", notes)
            self.assertIn("Source: https://commons.wikimedia.org/wiki/File:", notes)
            self.assertIn("https://creativecommons.org/licenses/by/4.0/", notes)
            self.assertIn("Changes: ", notes)
        marker_texts = [shape._element.nvPicPr.cNvPr.get("descr")
                        for slide in rendered.slides for shape in slide.shapes
                        if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE
                        and str(shape._element.nvPicPr.cNvPr.get("name", "")).startswith("WENGAO_SCENE|")]
        self.assertEqual(len(marker_texts), 2)
        self.assertTrue(all("CC BY 4.0" in text and "Creator / attribution:" in text
                            and "Depiction: Illustrative scene" in text
                            and "Illustration basis:" in text
                            for text in marker_texts))

    def test_photo_provider_rejects_noncommercial_or_unrelated_results(self):
        relevant = {"provider": "fixture", "source_key": "right", "title": "Ceramic mug",
                    "description": "A ceramic cup on a table", "license": "CC0 1.0",
                    "source_page": "https://commons.wikimedia.org/wiki/File:right.png",
                    "download_url": "https://images.example.org/right.png"}
        noncommercial = {"provider": "fixture", "source_key": "nc", "title": "Ceramic mug",
                         "description": "Mug", "license": "CC BY-SA 4.0",
                         "download_url": "https://images.example.org/nc.png"}
        unrelated = {"provider": "fixture", "source_key": "wrong", "title": "Mountain landscape",
                     "description": "Wide mountains", "license": "CC0",
                     "download_url": "https://images.example.org/wrong.png"}

        class Search:
            name = "fixture"
            allowed_hosts = {"images.example.org"}

            def search(self, query):
                return [noncommercial, unrelated, relevant]

        photo_bytes = png_bytes(5)
        with tempfile.TemporaryDirectory() as cache:
            provider = ScenePhotoProvider(cache, providers=[Search()])
            with patch("wengao_ppt.scene_images._download_image",
                       return_value=(photo_bytes, "PNG", (800, 600))):
                result = provider.fetch_one("ceramic mug drinking water")
        self.assertEqual(result["source_key"], "fixture:right")
        self.assertEqual(result["license"], "CC0 1.0")
        self.assertTrue(is_allowed_license("Public domain"))
        self.assertFalse(is_allowed_license("CC BY-SA 4.0"))

    def test_visual_review_model_quota_error_remains_actionable(self):
        relevant = {
            "provider": "fixture", "source_key": "fixture:cup",
            "title": "Person drinking water from a ceramic cup",
            "description": "A person drinks water from a ceramic cup at home",
            "creator": "Fixture photographer", "attribution": "Photo by Fixture photographer",
            "source_page": "https://commons.wikimedia.org/wiki/File:cup.png",
            "license": "CC0 1.0", "license_version": "1.0",
            "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
            "download_url": "https://images.example.org/cup.png",
        }

        class Search:
            name = "fixture"
            allowed_hosts = {"images.example.org"}

            def search(self, query):
                return [relevant]

        photo_bytes = png_bytes(7)

        def quota_failure(_scene, _image_bytes):
            raise SceneModelError("MODEL.QUOTA_EXCEEDED", "fixture quota exhausted")

        with tempfile.TemporaryDirectory() as cache:
            provider = ScenePhotoProvider(
                cache, providers=[Search()], visual_reviewer=quota_failure)
            with patch("wengao_ppt.scene_images._download_image",
                       return_value=(photo_bytes, "PNG", (800, 600))):
                with self.assertRaises(SceneModelError) as caught:
                    provider.fetch_one("ceramic cup drinking water")
        self.assertEqual(caught.exception.code, "MODEL.QUOTA_EXCEEDED")

    def test_transient_provider_timeout_does_not_disable_provider_for_later_scenes(self):
        class Search:
            name = "fixture"
            allowed_hosts = {"images.example.org"}

            def __init__(self):
                self.calls = 0

            def search(self, query):
                self.calls += 1
                raise SceneImageError("SCENE.PROVIDER_UNAVAILABLE", "simulated timeout")

        with tempfile.TemporaryDirectory() as cache:
            search = Search()
            provider = ScenePhotoProvider(cache, providers=[search])
            for query in ("shopper choosing groceries supermarket",
                          "older adults walking park"):
                with self.assertRaises(SceneImageError):
                    provider.fetch_one(query)
        self.assertEqual(search.calls, 2)
        self.assertNotIn("fixture", provider.disabled_providers)

    def test_photo_provider_skips_explicit_metadata_and_accepts_a_safe_relevant_photo(self):
        explicit = {"provider": "fixture", "source_key": "unsafe",
                    "title": "Nude woman drinking tea", "description": "Nude art study",
                    "license": "CC0", "download_url": "https://images.example.org/unsafe.png"}
        safe = {"provider": "fixture", "source_key": "safe",
                "title": "Person drinking a cup of tea",
                "description": "A person drinking tea from a cup at home",
                "license": "CC0",
                "source_page": "https://commons.wikimedia.org/wiki/File:safe.png",
                "download_url": "https://images.example.org/safe.png"}

        class Search:
            name = "fixture"
            allowed_hosts = {"images.example.org"}

            def search(self, query):
                return [explicit, safe]

        with tempfile.TemporaryDirectory() as cache:
            provider = ScenePhotoProvider(cache, providers=[Search()])
            with patch("wengao_ppt.scene_images._download_image",
                       return_value=(png_bytes(6), "PNG", (800, 600))) as download:
                result = provider.fetch_one("person drinking tea from a cup")
        self.assertEqual(result["source_key"], "fixture:safe")
        self.assertEqual(download.call_count, 1)
        self.assertEqual(download.call_args.args[0], "https://images.example.org/safe.png")

    def test_provider_refuses_weak_metadata_relevance_without_downloading(self):
        weak = {"provider": "fixture", "source_key": "weak", "title": "A person",
                "description": "A portrait in a room", "license": "CC0",
                "download_url": "https://images.example.org/weak.png"}

        class Search:
            name = "fixture"
            allowed_hosts = {"images.example.org"}

            def search(self, query):
                return [weak]

        with tempfile.TemporaryDirectory() as cache:
            provider = ScenePhotoProvider(cache, providers=[Search()])
            with patch("wengao_ppt.scene_images._download_image") as download:
                with self.assertRaises(SceneImageError) as caught:
                    provider.fetch_one("person drinking tea from a cup")
        # The provider now separates "no rows" from "rows returned but none
        # eligible": a weak-relevance rejection is the latter, and reporting it
        # as "no licensed photo" hid the real reason.
        self.assertEqual(caught.exception.code, "SCENE.PROVIDER_ROWS_INELIGIBLE")
        self.assertTrue(any(x["code"] == "SCENE.IMAGE_RELEVANCE_TOO_WEAK"
                            for x in provider.errors))
        download.assert_not_called()

    def test_metadata_relevance_is_a_bounded_fraction_of_all_query_terms(self):
        query = "MacBook Air iPhone desk"
        self.assertEqual(metadata_relevance(query, {
            "title": "Late 2010 MacBook Air",
            "description": "A desk set containing an iPhone and MacBook Air.",
        }), 1.0)
        self.assertEqual(metadata_relevance(query, {
            "title": "MacBook",
            "description": "An iPhone.",
        }), 0.5)

    def test_scene_photo_gate_rejects_recording_and_historical_artwork_false_matches(self):
        recording = {
            "title": "U.S. Air Force Pipe Band recording session",
            "description": "A studio recording of musicians. " + ("archive notes " * 100),
        }
        recording["description"] += " Office staff completed a checklist."
        query = "employee documenting completion checklist office"
        self.assertFalse(scene_metadata_matches_query(query, recording))

        historical_artwork = {
            "title": "Illustration of a Crimea bedroom, 1869",
            "description": "An engraved drawing showing soldiers sleeping in a room.",
        }
        morning_query = "young adult stretching bedroom morning"
        self.assertFalse(scene_metadata_matches_query(morning_query, historical_artwork))
        self.assertFalse(photo_style_matches_metadata("documentary photography",
                                                      historical_artwork))

        relevant_photo = {
            "title": "Young adult stretching beside a bedroom window in morning light",
            "description": "A person wakes and stretches in a modern bedroom.",
        }
        self.assertTrue(scene_metadata_matches_query(morning_query, relevant_photo))
        self.assertTrue(photo_style_matches_metadata("documentary photography",
                                                     relevant_photo))

    def test_scene_photo_provider_skips_recording_studio_false_positive(self):
        anchors = {"subject": "staff", "action": "documenting completion checklist",
                   "setting": "workplace"}
        query = "staff documenting completion checklist workplace"
        self.assertTrue(query_preserves_photo_search_anchors(query, anchors))
        self.assertFalse(query_preserves_photo_search_anchors(
            "staff recording completion status next steps", anchors))
        music_photo = {
            "provider": "fixture", "source_key": "music-recording-studio",
            "title": "STAFF Sergeant Jeff Bare creates an overdub at a recording studio",
            "description": "A pipe band records its first album. Office staff completed a checklist.",
            "download_url": "https://images.example.org/music.png",
            "license": "CC0 1.0", "width": 1600, "height": 900,
        }
        relevant_photo = {
            "provider": "fixture", "source_key": "workplace-checklist",
            "title": "Staff documenting completion checklist at workplace",
            "description": "An employee records a completed task checklist at an office workstation.",
            "source_page": "https://commons.wikimedia.org/wiki/File:workplace-checklist.png",
            "download_url": "https://images.example.org/checklist.png",
            "license": "CC0 1.0", "width": 1600, "height": 900,
        }

        class Search:
            name = "fixture"
            allowed_hosts = {"images.example.org"}

            def search(self, provider_query):
                return [music_photo, relevant_photo]

        with tempfile.TemporaryDirectory() as cache:
            provider = ScenePhotoProvider(cache, providers=[Search()])
            context = {"style": "documentary photography",
                       "photo_search_terms": anchors}
            with patch("wengao_ppt.scene_images._download_image",
                       return_value=(png_bytes(11), "PNG", (800, 600))) as download:
                result = provider.fetch_one(query, scene_context=context)
        self.assertEqual(result["source_key"], "fixture:workplace-checklist")
        self.assertEqual(download.call_args.args[0], "https://images.example.org/checklist.png")

    def test_scene_photo_metadata_accepts_grounded_caption_synonyms_by_role(self):
        brushing = {
            "subject": "person", "action": "brushing teeth", "setting": "bathroom",
        }
        query = "person brushing teeth bathroom"
        caption = {
            "title": "Woman brushing her teeth at a bathroom mirror",
            "description": "Adult performs oral hygiene at the sink before work.",
        }
        context = {"photo_search_terms": brushing}
        self.assertTrue(scene_metadata_matches_query(query, caption, scene_context=context))
        self.assertEqual(scene_metadata_relevance(query, caption, scene_context=context), 1.0)

    def test_scene_photo_metadata_accepts_common_lived_scene_synonyms(self):
        cases = [
            ({"subject": "shopper", "action": "selecting produce", "setting": "grocery store"},
             "shopper selecting produce grocery store",
             {"title": "Customer choosing vegetables at a supermarket",
              "description": "A grocery shopper selects fresh vegetables in a produce market."}),
            ({"subject": "factory worker", "action": "assembling products", "setting": "production line"},
             "factory worker assembling products production line",
             {"title": "Assembly worker assembling products on a production line",
              "description": "Workers assemble components on a factory floor."}),
            ({"subject": "courier", "action": "carrying food", "setting": "apartment doorway"},
             "courier carrying food apartment doorway",
             {"title": "Delivery rider bringing a meal to an apartment entrance",
              "description": "A food courier delivers an order at the front door."}),
            ({"subject": "students", "action": "reading lesson", "setting": "classroom"},
             "students reading lesson classroom",
             {"title": "Pupils studying a lesson in a schoolroom",
              "description": "Students read lesson material in a classroom."}),
        ]
        for anchors, query, record in cases:
            with self.subTest(anchors=anchors):
                context = {"photo_search_terms": anchors}
                self.assertTrue(scene_metadata_matches_query(query, record,
                                                             scene_context=context))
                self.assertEqual(scene_metadata_relevance(query, record,
                                                          scene_context=context), 1.0)

    def test_neutral_photo_search_query_bounds_synonym_reformulations(self):
        self.assertTrue(is_neutral_photo_search_query(
            "physician examining patient hospital"))
        for query in ("病人在医院挂号", "worker assembling vehicle 2025 factory",
                      "https://example.org worker assembling parts factory",
                      "one two three", "low quality blurry 8k photo image" ):
            with self.subTest(query=query):
                self.assertFalse(is_neutral_photo_search_query(query))

        workplace = {
            "subject": "staff", "action": "verifying task requirements",
            "setting": "workplace",
        }
        query = "staff verifying task requirements workplace"
        caption = {
            "title": "Employee reviewing a checklist at an office workstation",
            "description": "A clerk confirms the task requirements at a desk.",
        }
        context = {"photo_search_terms": workplace}
        self.assertTrue(scene_metadata_matches_query(query, caption, scene_context=context))
        self.assertEqual(scene_metadata_relevance(query, caption, scene_context=context), 1.0)

    def test_scene_photo_metadata_does_not_join_unrelated_caption_sentences(self):
        anchors = {"subject": "staff", "action": "documenting completion checklist",
                   "setting": "workplace"}
        query = "staff documenting completion checklist workplace"
        record = {
            "title": "U.S. Air Force Pipe Band recording session",
            "description": "Musicians record their first album in a studio. "
                           "Office staff completed a checklist.",
        }
        self.assertFalse(scene_metadata_matches_query(
            query, record, scene_context={"photo_search_terms": anchors}))

    def test_scene_photo_metadata_may_combine_title_with_one_caption_sentence(self):
        anchors = {"subject": "worker", "action": "assembling parts",
                   "setting": "factory"}
        record = {
            "title": "Connolly Shoe Factory",
            "description": "Workers cutting parts and assembling. "
                           "Archive accession details and unrelated notes.",
        }
        self.assertTrue(scene_metadata_matches_query(
            "worker assembling parts factory", record,
            scene_context={"photo_search_terms": anchors}))

    def test_scene_metadata_recognizes_common_silent_e_action_inflection(self):
        anchors = {"subject": "employee", "action": "using laptop",
                   "setting": "office"}
        record = {
            "title": "Office employee",
            "description": "An employee uses a laptop at a desk.",
        }
        self.assertTrue(scene_metadata_matches_query(
            "employee using laptop office", record,
            scene_context={"photo_search_terms": anchors}))

    def test_wikimedia_search_unions_bounded_query_variants(self):
        provider = WikimediaCommonsPhotoProvider()
        first = {"source_key": "File:shared.jpg"}
        second = {"source_key": "File:from-alternate.jpg"}

        def fake_search(query, limit):
            if query == "person brushing teeth bathroom":
                return [first]
            if query == "person brushing teeth":
                return [first, second]
            return []

        with patch.object(provider, "_search_query", side_effect=fake_search) as search:
            result = provider.search("person brushing teeth bathroom", limit=16)
        self.assertGreater(search.call_count, 1)
        self.assertEqual([row["source_key"] for row in result],
                         ["File:shared.jpg", "File:from-alternate.jpg"])

    def test_scene_search_query_variants_preserve_all_three_roles(self):
        base = "person brushing teeth bathroom"
        anchors = {"subject": "person", "action": "brushing teeth", "setting": "bathroom"}
        variants = scene_search_query_variants(base, {"photo_search_terms": anchors})
        self.assertIn("woman brushing teeth mirror", variants)
        self.assertLessEqual(len(variants), 6)
        for query in variants:
            self.assertGreaterEqual(len(query.split()), 4)
            self.assertLessEqual(len(query.split()), 7)
        self.assertIn("young adult morning stretch bedroom", scene_search_query_variants(
            "young adult stretching arms bedroom",
            {"photo_search_terms": {"subject": "young adult", "action": "stretching arms",
                                    "setting": "bedroom"}}))
        grocery = scene_search_query_variants(
            "shopper selecting produce grocery store",
            {"photo_search_terms": {"subject": "shopper", "action": "selecting produce",
                                    "setting": "grocery store"}}, max_variants=4)
        self.assertTrue(any("shopping" in query or "choosing" in query
                            for query in grocery))

    def test_scene_search_reserves_role_synonyms_when_planner_has_many_variants(self):
        canonical = "person brushing teeth bathroom"
        context = {
            "photo_search_terms": {
                "subject": "person", "action": "brushing teeth", "setting": "bathroom",
            },
            "visual_query": canonical,
            "visual_queries": [
                "adult brushing teeth sink", "woman brushing teeth washroom",
                "person oral hygiene mirror",
            ],
            "metadata_query": canonical,
        }
        variants = scene_search_query_variants(canonical, context, max_variants=4)
        self.assertEqual(variants[0], canonical)
        # Planner alternatives cannot crowd out all deterministic provider
        # vocabulary fallbacks when the provider budget is intentionally small.
        self.assertIn("woman brushing teeth mirror", variants)
        self.assertLessEqual(len(variants), 4)

    def test_multiword_scene_roles_accept_specific_provider_synonyms_only(self):
        query = "factory worker assembling products production line"
        context = {"photo_search_terms": {
            "subject": "factory worker", "action": "assembling products",
            "setting": "production line",
        }}
        relevant = {
            "title": "Production worker assembling products on an assembly line",
            "description": "Factory staff assemble components on a production line.",
        }
        unrelated = {
            "title": "Production worker at an office desk",
            "description": "A worker checks a calendar beside a computer.",
        }
        self.assertTrue(scene_metadata_matches_query(query, relevant, scene_context=context))
        self.assertFalse(scene_metadata_matches_query(query, unrelated, scene_context=context))

    def test_realistic_grocery_and_aging_captions_match_narrow_scene_roles(self):
        grocery_query = "shopper choosing groceries supermarket"
        grocery_context = {"photo_search_terms": {
            "subject": "shopper", "action": "choosing groceries", "setting": "supermarket",
        }}
        grocery_caption = {
            "title": "Agriculture Research Service",
            "description": (
                "Parents involve youngsters in helping to select items from the supermarket "
                "produce section."
            ),
        }
        unrelated_grocery = {
            "title": "Parents and children at home",
            "description": "The family eats vegetables. A supermarket nearby is open late.",
        }
        self.assertTrue(scene_metadata_matches_query(
            grocery_query, grocery_caption, scene_context=grocery_context))
        self.assertFalse(scene_metadata_matches_query(
            grocery_query, unrelated_grocery, scene_context=grocery_context))

        aging_query = "older adults walking together park"
        aging_context = {"photo_search_terms": {
            "subject": "older adults", "action": "walking together", "setting": "park",
        }}
        aging_caption = {
            "title": "Seniors walk together in a city park",
            "description": "Older people enjoy an afternoon stroll outdoors.",
        }
        self.assertTrue(scene_metadata_matches_query(
            aging_query, aging_caption, scene_context=aging_context))

    def test_planner_synonyms_expand_search_without_weakening_canonical_gate(self):
        canonical = "shopper selecting produce grocery store"
        alternate = "customer shopping groceries supermarket"
        anchors = {"subject": "shopper", "action": "selecting produce",
                   "setting": "grocery store"}
        context = {"photo_search_terms": anchors, "visual_query": canonical,
                   "visual_queries": [canonical, alternate],
                   "metadata_query": canonical}
        variants = scene_search_query_variants(canonical, context, max_variants=4)
        self.assertIn(alternate, variants)

        record = {"provider": "wikimedia-commons", "source_key": "File:loose-topic.jpg",
                  "title": "Customer shopping at a supermarket",
                  "description": "A shopper visits a grocery store.",
                  "license": "Public domain", "license_url": ""}
        seen = []

        class QueryAwareProvider:
            name = "wikimedia-commons"
            allowed_hosts = {"commons.wikimedia.org", "upload.wikimedia.org"}

            def search_scene(self, query, *, scene_context, limit=16):
                seen.extend(scene_search_query_variants(query, scene_context, max_variants=4))
                return [record]

        provider = ScenePhotoProvider(self.root / "synonym-query-cache",
                                      providers=[QueryAwareProvider()])
        with self.assertRaises(SceneImageError):
            provider.fetch_one(canonical, scene_context=context)
        self.assertIn(alternate, seen)
        self.assertFalse(scene_metadata_matches_query(canonical, record,
                                                      scene_context=context))

    def test_wikimedia_scene_search_finds_role_synonym_variant(self):
        provider = WikimediaCommonsPhotoProvider()
        record = {"source_key": "File:woman-brushing-teeth.jpg",
                  "title": "Woman brushing teeth",
                  "description": "An adult brushes her teeth while looking in a bathroom mirror."}
        calls = []

        def fake_search(query, limit, **kwargs):
            calls.append(query)
            return [record] if query == "woman brushing teeth mirror" else []

        context = {"photo_search_terms": {"subject": "person", "action": "brushing teeth",
                                          "setting": "bathroom"}}
        with patch.object(provider, "_search_query", side_effect=fake_search):
            results = provider.search_scene("person brushing teeth bathroom",
                                            scene_context=context)
        self.assertIn("woman brushing teeth mirror", calls)
        self.assertEqual(len(results), 1)
        self.assertTrue(scene_metadata_matches_query(
            "person brushing teeth bathroom", results[0], scene_context=context))

    def test_wikimedia_scene_search_obeys_provider_wide_budget(self):
        provider = WikimediaCommonsPhotoProvider()
        anchors = {"subject": "person", "action": "brushing teeth", "setting": "bathroom"}
        clock = [0.0]
        calls = []

        def monotonic():
            return clock[0]

        def fake_search(query, limit, *, timeout_s, max_attempts):
            calls.append({"query": query, "timeout_s": timeout_s,
                          "max_attempts": max_attempts})
            clock[0] += timeout_s
            return []

        with patch.object(provider, "_search_query", side_effect=fake_search), \
                patch("wengao_ppt.scene_images.time.monotonic", side_effect=monotonic):
            with self.assertRaises(SceneImageError) as raised:
                provider.search_scene(
                    "person brushing teeth bathroom",
                    scene_context={"photo_search_terms": anchors})

        self.assertEqual(raised.exception.code, "SCENE.SEARCH_BUDGET_EXHAUSTED")
        self.assertLessEqual(len(calls), 4)
        self.assertTrue(all(
            row["timeout_s"] <= scene_images.SCENE_SEARCH_QUERY_TIMEOUT_BY_PROVIDER[
                "wikimedia-commons"] for row in calls))
        self.assertTrue(all(row["max_attempts"] == 2 for row in calls))

    def test_two_distinct_scene_photos_share_a_safe_reflowed_bottom_row(self):
        source = Presentation()
        # Paired placements are designed for the product's 16:9 slide grid.
        # python-pptx's blank default is 4:3, which correctly has no such row.
        source.slide_width = Inches(13.333)
        source.slide_height = Inches(7.5)
        slide = source.slides.add_slide(source.slide_layouts[6])
        title = slide.shapes.add_textbox(Inches(.7), Inches(.55), Inches(11.9), Inches(1.15))
        title.text_frame.text = "现场核对与结果记录"
        for paragraph in title.text_frame.paragraphs:
            for run in paragraph.runs:
                run.font.size = Pt(38)
        body = slide.shapes.add_textbox(Inches(.7), Inches(1.9), Inches(11.9), Inches(5.1))
        body.name = "Content Placeholder 2"
        body.text_frame.word_wrap = True
        paragraphs = [
            "员工在办公室核对任务要求和现场材料。",
            "对于方案中的关键动作，要按清单逐项检查并记录结果。",
            "现场核对可以发现执行偏差，及时补充所需信息。",
            "完成核对后，员工记录任务状态，形成可追溯的过程凭证。",
        ]
        for index, text in enumerate(paragraphs):
            paragraph = body.text_frame.paragraphs[0] if index == 0 else body.text_frame.add_paragraph()
            paragraph.text = text
            paragraph.line_spacing = 1.25
            paragraph.space_after = Pt(10)
            for run in paragraph.runs:
                run.font.size = Pt(24)
        source_path = self.root / "paired-scenes-source.pptx"
        source.save(source_path)

        scenes = [
            {
                "scene_key": "employee_verifying_requirements",
                "canonical_scene_id": "employee_verifying_requirements_office",
                "topic": "任务核对", "subject": "员工", "action": "核对任务要求",
                "setting": "办公室", "label": "员工核对任务要求",
                "lighting": "办公室均匀环境光", "composition_camera": "中景横向构图",
                "style": "办公室纪实摄影", "quality": "清晰自然细节",
                "visual_query": "employee verifying checklist office",
                "page_candidates": [{"page": 1, "relevance": .96,
                                     "evidence": [paragraphs[0]]}],
            },
            {
                "scene_key": "employee_recording_completion",
                "canonical_scene_id": "employee_recording_completion_office",
                "topic": "任务核对", "subject": "员工", "action": "记录完成状态",
                "setting": "办公室", "label": "员工记录完成状态",
                "lighting": "办公室均匀环境光", "composition_camera": "中景横向构图",
                "style": "办公室纪实摄影", "quality": "清晰自然细节",
                "visual_query": "employee recording completion office",
                "page_candidates": [{"page": 1, "relevance": .94,
                                     "evidence": [paragraphs[3]]}],
            },
        ]
        resolution = {
            "scenes": scenes,
            "page_coverage": [{"page": 1,
                               "scene_keys": [row["scene_key"] for row in scenes],
                               "no_scene_reason": None,
                               "evidence": [paragraphs[0], paragraphs[3]]}],
        }
        records = {}
        for key, seed, title_text, description in (
                ("employee_verifying_requirements", 4,
                 "Employee verifying a task checklist in an office",
                 "An employee reviews requirements at an office workstation."),
                ("employee_recording_completion", 6,
                 "Employee recording completion status in an office",
                 "A worker documents finished tasks at an office desk.")):
            image = Image.new("RGB", (1600, 900), (20 + seed * 13, 35, 50))
            draw = ImageDraw.Draw(image)
            draw.rectangle((seed * 31, 80, 650 + seed * 10, 810), fill=(210, 130 + seed * 3, 55))
            draw.ellipse((800, 120 + seed * 5, 1450, 790), fill=(75, 150 + seed * 4, 180))
            image_path = self.assets / f"{key}.png"
            image.save(image_path)
            data = image_path.read_bytes()
            with Image.open(io.BytesIO(data)) as decoded:
                width, height = decoded.size
            records[key] = {
                "provider": "fixture", "source_key": f"fixture:{key}",
                "title": title_text, "description": description,
                "creator": "Fixture photographer", "attribution": title_text,
                "source_page": f"https://commons.wikimedia.org/wiki/File:{key}.png",
                "download_url": "https://images.example.org/photo.png",
                "license": "CC0 1.0", "license_version": "1.0",
                "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
                "file": str(image_path), "sha256": hashlib.sha256(data).hexdigest(),
                "perceptual_hash": perceptual_hash(data), "bytes": len(data),
                "actual_format": "PNG", "actual_width": width, "actual_height": height,
            }

        class PairProvider:
            def fetch_one(_self, query, **kwargs):
                context = kwargs["scene_context"]
                return records[context["scene_key"]]

        plan = semantic_scenes.build_plan(source_path, provider=PairProvider(),
                                          semantic_resolution=resolution)
        self.assertEqual(plan["unresolved_scenes"], [], plan["unresolved_scenes"])
        self.assertEqual(set(plan["images"]), {row["scene_key"] for row in scenes})
        specs = list(plan["images"].values())
        self.assertEqual(len({spec["sha256"] for spec in specs}), 2)
        self.assertTrue(all(spec["placement_mode"] == "paired_photo_row" for spec in specs))
        self.assertTrue(all(spec["fit_mode"] == "contain" for spec in specs))
        self.assertTrue(all(spec["reflow_shapes"] for spec in specs))
        left, right = sorted(specs, key=lambda spec: spec["rect"][0])
        self.assertLessEqual(left["rect"][0] + left["rect"][2], right["rect"][0])
        self.assertGreaterEqual(min(spec["area_ratio"] for spec in specs), 0.12)
        self.assertEqual(left["reflow_shapes"], right["reflow_shapes"])

        output_path = self.root / "paired-scenes-candidate.pptx"
        semantic_scenes.apply_plan(source_path, plan, output_path)
        plan_path = self.root / "paired-scenes-plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        report = semantic_scenes.validate_output(source_path, output_path, plan_path)
        self.assertEqual(report["status"], "EXECUTED", report["findings"])
        rendered = Presentation(str(output_path))
        body_out = next(shape for shape in rendered.slides[0].shapes
                        if shape.name == "Content Placeholder 2")
        self.assertEqual([p.text for p in body_out.text_frame.paragraphs], paragraphs)
        self.assertEqual([round(run.font.size.pt, 2) for p in body_out.text_frame.paragraphs
                          for run in p.runs], [24.0] * len(paragraphs))
        self.assertAlmostEqual(body_out.top / Inches(1), 1.76, delta=.01)
        self.assertLess(body_out.height / Inches(1), 2.6)

    def test_plan_recomputes_provider_relevance_instead_of_freezing_claimed_score(self):
        class StaleScoreProvider(FakePhotoProvider):
            def fetch_one(self, query, **kwargs):
                record = super().fetch_one(query, **kwargs)
                record["metadata_relevance"] = 1.3333
                return record

        plan = semantic_scenes.build_plan(
            self.source, provider=StaleScoreProvider(self.images),
            semantic_resolution=semantic_fixture())
        self.assertFalse(plan["unresolved_scenes"])
        scene_by_key = {row["scene_key"]: row for row in plan["scenes"]}
        for spec in plan["images"].values():
            self.assertLessEqual(spec["metadata_relevance"], 1.0)
            self.assertAlmostEqual(spec["metadata_relevance"],
                                   scene_metadata_relevance(
                                       spec["visual_query"], spec,
                                       scene_context=scene_by_key[spec["scene_key"]]))

    def test_gate_rechecks_photo_scene_relevance_from_frozen_metadata(self):
        provider = FakePhotoProvider(self.images)
        plan = semantic_scenes.build_plan(self.source, provider=provider,
                                          semantic_resolution=semantic_fixture())
        output = self.root / "unrelated-photo-metadata.pptx"
        semantic_scenes.apply_plan(self.source, plan, output)
        plan["images"]["hand_drinking_from_ceramic_cup"]["title"] = "Mountain landscape"
        plan["images"]["hand_drinking_from_ceramic_cup"]["description"] = "Wide mountain view"
        plan["plan_sha256"] = semantic_scenes._plan_hash(plan)
        plan_path = self.root / "unrelated-photo-plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        report = semantic_scenes.validate_output(self.source, output, plan_path)
        self.assertEqual(report["status"], "FAILED")
        self.assertIn("SCENE.IMAGE_RELEVANCE_UNVERIFIED",
                      {row["code"] for row in report["findings"]})

    def test_gate_requires_real_creator_or_attribution_metadata(self):
        provider = FakePhotoProvider(self.images)
        plan = semantic_scenes.build_plan(self.source, provider=provider,
                                          semantic_resolution=semantic_fixture())
        output = self.root / "missing-attribution.pptx"
        semantic_scenes.apply_plan(self.source, plan, output)
        plan["images"]["hand_drinking_from_ceramic_cup"]["creator"] = ""
        plan["images"]["hand_drinking_from_ceramic_cup"]["attribution"] = ""
        plan["plan_sha256"] = semantic_scenes._plan_hash(plan)
        plan_path = self.root / "missing-attribution-plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        report = semantic_scenes.validate_output(self.source, output, plan_path)
        self.assertEqual(report["status"], "FAILED")
        self.assertIn("SCENE.LICENSE_METADATA_MISSING",
                      {row["code"] for row in report["findings"]})

    def test_gate_accepts_attributed_pexels_photo_and_rejects_non_pexels_source_page(self):
        provider = FakePhotoProvider(self.images)

        def pexels_plan():
            plan = semantic_scenes.build_plan(
                self.source, provider=provider, semantic_resolution=semantic_fixture())
            for index, spec in enumerate(plan["images"].values(), 1):
                spec.update({
                    "provider": "pexels",
                    "source_key": str(100 + index),
                    "source_page": f"https://www.pexels.com/photo/example-photo-{100 + index}/",
                    "creator": "Fixture photographer",
                    "attribution": "Photo by Fixture photographer on Pexels",
                    "license": "Pexels License",
                    "license_version": "current",
                    "license_url": "https://www.pexels.com/license/",
                })
            plan["plan_sha256"] = semantic_scenes._plan_hash(plan)
            return plan

        valid_plan = pexels_plan()
        valid_output = self.root / "pexels-attributed-candidate.pptx"
        semantic_scenes.apply_plan(self.source, valid_plan, valid_output)
        valid_path = self.root / "pexels-attributed-plan.json"
        valid_path.write_text(json.dumps(valid_plan, ensure_ascii=False), encoding="utf-8")
        valid_report = semantic_scenes.validate_output(self.source, valid_output, valid_path)
        self.assertEqual(valid_report["status"], "EXECUTED", valid_report["findings"])

        invalid_plan = pexels_plan()
        invalid_plan["images"]["hand_drinking_from_ceramic_cup"]["source_page"] = (
            "https://example.org/photo-not-hosted-by-pexels")
        invalid_plan["plan_sha256"] = semantic_scenes._plan_hash(invalid_plan)
        invalid_output = self.root / "pexels-invalid-source-candidate.pptx"
        semantic_scenes.apply_plan(self.source, invalid_plan, invalid_output)
        invalid_path = self.root / "pexels-invalid-source-plan.json"
        invalid_path.write_text(json.dumps(invalid_plan, ensure_ascii=False), encoding="utf-8")
        invalid_report = semantic_scenes.validate_output(
            self.source, invalid_output, invalid_path)
        self.assertEqual(invalid_report["status"], "FAILED")
        self.assertIn("SCENE.LICENSE_METADATA_MISSING",
                      {row["code"] for row in invalid_report["findings"]})

    def test_sensitive_metadata_guard_does_not_block_neutral_naked_eye_phrase(self):
        from wengao_ppt.scene_images import is_safe_metadata
        self.assertFalse(is_safe_metadata({"title": "Nude woman drinking tea"}))
        self.assertFalse(is_safe_metadata({"description": "Graphic medical injury"}))
        self.assertTrue(is_safe_metadata({"title": "Naked mole rat at a feeding station"}))

    def test_transparent_text_placeholder_reflows_for_scene_photo(self):
        src_path = self.root / "fullwidth_text_slide.pptx"
        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        title = slide.shapes.add_textbox(Inches(0.7), Inches(0.55), Inches(11.9), Inches(1.15))
        title.text_frame.text = "智慧医院数字化建设"
        body = slide.shapes.add_textbox(Inches(0.7), Inches(1.9), Inches(11.9), Inches(5.1))
        body.name = "Content Placeholder 2"
        body.text_frame.word_wrap = True
        body.text_frame.text = "一只手拿着陶瓷杯喝水的场景，说明日常饮水。"
        for p_text in ["高血压与糖尿病规范化管理率达到88.5%。", "便携式智能监测设备累计配发35万台。"]:
            p = body.text_frame.add_paragraph()
            p.text = p_text
        prs.save(src_path)

        provider = FakePhotoProvider(self.images)
        plan = semantic_scenes.build_plan(
            src_path, provider=provider,
            semantic_resolution={
                "scenes": [{
                    "scene_key": "hand_drinking_from_ceramic_cup",
                    "canonical_scene_id": "hand_drinking_from_ceramic_cup",
                    "topic": "日常饮水", "subject": "手与陶瓷杯", "action": "拿着陶瓷杯喝水",
                    "setting": "室内办公区", "label": "陶瓷杯饮水",
                    "visual_query": "person drinking water from a ceramic mug",
                    "visual_query_alternatives": ["ceramic mug water drinking"],
                    "page_candidates": [{"page": 1, "relevance": 0.95,
                                         "evidence": ["一只手拿着陶瓷杯喝水的场景"]}]
                }],
                "page_coverage": [{"page": 1, "scene_keys": ["hand_drinking_from_ceramic_cup"],
                                   "no_scene_reason": None,
                                   "evidence": ["一只手拿着陶瓷杯喝水的场景"]}]
            }
        )
        self.assertFalse(plan.get("unresolved_scenes"))
        self.assertIn("hand_drinking_from_ceramic_cup", plan.get("images", {}))
        out_path = self.root / "fullwidth_text_slide.out.pptx"
        semantic_scenes.apply_plan(src_path, plan, out_path)
        plan_path = self.root / "fullwidth_plan.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        report = semantic_scenes.validate_output(src_path, out_path, plan_path)
        self.assertEqual(report["status"], "EXECUTED")
        self.assertEqual(report["findings"], [])

        # Verify the text box width was narrowed so it cleanly separates from the photo
        prs_out = Presentation(str(out_path))
        body_out = next(s for s in prs_out.slides[0].shapes if s.name == "Content Placeholder 2")
        self.assertLess(body_out.width / Inches(1), 10.0)
        self.assertGreaterEqual(body_out.width / Inches(1), 4.2)

    def test_normalize_semantics_accepts_singular_scene_key_and_filters_invalid_tokens(self):
        source_pages = [{"page": 1, "text": "一只手拿着陶瓷杯喝水的场景，说明日常饮水。", "text_chars": 20}]
        raw = {
            "scenes": [{
                "scene_key": "hand_drinking_from_ceramic_cup",
                "canonical_scene_id": "hand_drinking_from_ceramic_cup",
                "topic": "日常饮水", "subject": "手与陶瓷杯", "action": "拿着陶瓷杯喝水",
                "setting": "室内办公区", "label": "陶瓷杯饮水",
                "visual_query": "person drinking water from a ceramic mug",
                "page_candidates": [{"page": 1, "relevance": 0.95, "evidence": ["一只手拿着陶瓷杯喝水的场景"]}]
            }],
            "page_coverage": [
                {"page": 1, "scene_key": "hand_drinking_from_ceramic_cup", "evidence": ["一只手拿着陶瓷杯喝水的场景"]},
                {"page": 1, "scene_keys": ["hand_drinking_from_ceramic_cup<think>bad token</think>"], "evidence": ["一只手拿着陶瓷杯喝水的场景"]}
            ]
        }
        scenes, cov = semantic_scenes._normalize_semantics(raw, source_pages)
        self.assertEqual(len(scenes), 1)
        self.assertEqual(cov[0]["scene_keys"], ["hand_drinking_from_ceramic_cup"])

    def test_frozen_gate_blocks_generic_no_scene_reason_even_with_grounded_excerpt(self):
        text = "消费的经济学定义包括满足生活需要的行为。"
        findings = []
        semantic_scenes._validate_page_coverage(
            {"page_coverage": [{"page": 1, "scene_keys": [],
                                "no_scene_reason": "本页为宏观概念论述或无具体实体场景",
                                "evidence": [text]}], "scenes": []},
            [{"page": 1, "text": text, "text_chars": len(text)}], findings)
        self.assertIn("SCENE.PAGE_NO_SCENE_REASON_INVALID",
                      {row["code"] for row in findings})


class BoundedMultilingualScenePlanningTests(unittest.TestCase):
    def test_truncated_batch_splits_and_reuses_one_cross_language_scene_identity(self):
        pages = []
        for page in range(1, 5):
            text = "কারখানার উৎপাদন লাইনে একজন শ্রমিক যন্ত্রাংশ সংযোজন করেন।"
            pages.append({
                "page": page, "title": "উৎপাদন-লাইন",
                "text": text,
                "language_hint": detect_language_hint(text),
            })
        english = "A worker assembles components on the factory production line."
        pages.append({
            "page": 5, "title": "Factory production line",
            "text": english,
            "language_hint": detect_language_hint(english),
        })

        scene_key = "worker_assembling_components_factory_line"
        canonical_id = "worker_assembling_components_factory_line"
        calls = []
        returned_plan_batches = 0

        def scene_for_pages(page_numbers):
            candidates = [
                {"page": page, "relevance": 0.95,
                 "evidence": [pages[page - 1]["text"]]}
                for page in page_numbers
            ]
            if page_numbers and page_numbers[0] == 5:
                subject, action, setting = "factory worker", "assembling components", "production line"
                label = "Worker on assembly line"
            else:
                subject, action, setting = "কারখানার শ্রমিক", "যন্ত্রাংশ সংযোজন", "উৎপাদন লাইন"
                label = "উৎপাদন লাইনে শ্রমিক"
            return {
                "scene_key": scene_key,
                "canonical_scene_id": canonical_id,
                "topic": "উৎপাদন",
                "depiction_type": "illustrative",
                "illustration_rationale": "উৎপাদন লাইনে শ্রমিকের কাজ কারখানার উৎপাদন বিষয়টি বোঝাতে সহায়ক, তবে এটি উৎসে বর্ণিত নির্দিষ্ট ঘটনার দাবি নয়।",
                "subject": subject,
                "action": action,
                "setting": setting,
                "label": label,
                "lighting": "soft industrial light",
                "composition_camera": "documentary medium view",
                "style": "realistic documentary photography",
                "quality": "clear natural detail",
                "photo_search_terms": {
                    "subject": "worker",
                    "action": "assembling parts",
                    "setting": "factory",
                },
                "visual_query": "worker assembling parts factory",
                "visual_query_alternatives": [],
                "page_candidates": candidates,
            }

        def model_call(model, prompt):
            nonlocal returned_plan_batches
            calls.append(prompt)
            if prompt.startswith("You are an independent second-pass semantic coverage auditor."):
                audit_payload = json.loads(prompt.split("Audit every page in this payload:\n", 1)[1])
                ledger = audit_payload["expected_page_ledger"]
                return {
                    "response": json.dumps({
                        "schema_version": "1.2.0",
                        "review_status": "NO_GAPS",
                        "page_reviews": ledger,
                        "missing_scenes": [],
                        "merged_distinct_scenes": [],
                    }, ensure_ascii=False),
                    "model": "fixture-local-model",
                    "elapsed_seconds": 0.01,
                }

            payload = json.loads(
                prompt.split("Batch source data（包含页码、语言检测上下文与原文）：\n", 1)[1])
            page_numbers = payload["batch_context"]["included_original_page_numbers"]
            self.assertEqual(payload["batch_context"]["original_deck_page_count"], 5)
            self.assertEqual([int(row["page"]) for row in payload["source_pages"]], page_numbers)
            self.assertTrue(all(row["source_language_hint"] for row in payload["source_pages"]))
            if not returned_plan_batches:
                returned_plan_batches += 1
                return {
                    "response": "{",
                    "model": "fixture-local-model",
                    "done_reason": "length",
                    "eval_count": 4096,
                }
            returned_plan_batches += 1
            coverage = [
                {"page": page, "scene_keys": [scene_key], "no_scene_reason": None,
                 "evidence": [pages[page - 1]["text"]]}
                for page in page_numbers
            ]
            return {
                "response": json.dumps({
                    "schema_version": "2.2.0",
                    "scenes": [scene_for_pages(page_numbers)],
                    "page_coverage": coverage,
                }, ensure_ascii=False),
                "model": "fixture-local-model",
                "elapsed_seconds": 0.01,
            }

        resolved, meta = resolve_scene_semantics(
            pages, model="fixture-local-model", model_call=model_call)

        self.assertEqual(len(calls), 5)  # one truncation, three batches, one full-deck audit
        self.assertEqual(meta["planner_mode"], "bounded_page_batches")
        self.assertEqual(meta["planner_batch_page_limit"], 4)
        self.assertEqual(meta["truncation_splits"][0]["page_numbers"], [1, 2, 3, 4])
        self.assertEqual(meta["truncation_splits"][0]["split_into"], [[1, 2], [3, 4]])
        self.assertEqual(meta["coverage_audit"]["status"], "PASSED")
        self.assertEqual(meta["coverage_audit"]["pages_checked"], 5)
        self.assertEqual(len(resolved["scenes"]), 1)
        self.assertEqual(resolved["scenes"][0]["canonical_scene_id"], canonical_id)
        self.assertEqual(sorted(row["page"] for row in resolved["scenes"][0]["page_candidates"]),
                         [1, 2, 3, 4, 5])
        self.assertEqual([row["page"] for row in resolved["page_coverage"]], [1, 2, 3, 4, 5])
        self.assertTrue(all(row["scene_keys"] == [scene_key]
                            for row in resolved["page_coverage"]))


if __name__ == "__main__":
    unittest.main()
