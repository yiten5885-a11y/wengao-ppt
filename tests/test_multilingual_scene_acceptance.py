"""Independent acceptance tests for multilingual semantic scene images.

Scope of this file (offline evidence only):

1. the frozen 26-language scope is recognised from original text and marked
   per page before any keyword or scene extraction runs;
2. mixed-language, proper-noun, numeric, link-only and very short pages are
   reported honestly instead of being forced into Chinese or a guessed language;
3. a non-Chinese page is planned through the same open-vocabulary semantic path
   and receives a neutral English photo query that keeps the original meaning;
4. one concrete scene yields at most one photo per deck, while distinct
   sub-scenes of the same broad topic each receive a different photo;
5. an unrelated photo, a reused photo and a failed retrieval are recorded as
   failures and can never pass as a placed scene image.

The semantic planner result is injected here. These tests therefore prove the
routing, dedupe, placement and gate behaviour, and they explicitly do NOT prove
real model quality or real photo-provider retrieval. Those remain separate
acceptance items.
"""
import hashlib
import io
import json
import random
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.util import Inches

from wengao_ppt import semantic_scenes, visual_plan
from wengao_ppt.language_support import (
    CURRENT_LANGUAGE_ACCEPTANCE_CODES,
    detect_language_hint,
)
from wengao_ppt.scene_images import SceneImageError, perceptual_hash

FIXTURE = Path(__file__).parent / "fixtures" / "major-language-recognition-samples.json"


def png_bytes(seed: int) -> bytes:
    """Deterministic, visually distinct fixture photo bytes."""
    image = Image.new("RGB", (900, 600), (20 + seed * 17 % 200, 40, 60))
    draw = ImageDraw.Draw(image)
    for i in range(14):
        x = (i * 61 + seed * 29) % 880
        y = (i * 43 + seed * 31) % 580
        draw.rectangle((x, y, min(899, x + 20 + seed * 3), min(599, y + 30)),
                       fill=(180 + (i % 3) * 25, 120 + seed * 7 % 100, 60 + i * 4))
    grain = Image.frombytes(
        "RGB", (900, 600),
        random.Random(seed * 7919).randbytes(900 * 600 * 3))
    image = Image.blend(image, grain, 0.7)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def make_deck(path: Path, pages: list[tuple[str, str]]) -> Path:
    """Build a deck whose text frames are large enough to count as body text."""
    prs = Presentation()
    for title, body in pages:
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        title_box = slide.shapes.add_textbox(Inches(.6), Inches(.45), Inches(5.7), Inches(.7))
        title_box.text_frame.text = title
        body_box = slide.shapes.add_textbox(Inches(.6), Inches(1.4), Inches(5.6), Inches(3.0))
        body_box.text_frame.text = body
    prs.save(path)
    return path


class AcceptancePhotoProvider:
    """Fixture provider: metadata mirrors the neutral query, bytes are per-scene.

    It is not a network client. ``unrelated``/``fail``/``reuse`` modes exist so
    the negative acceptance cases can be exercised deterministically.
    """

    name = "fixture"
    allowed_hosts = {"images.example.org"}

    def __init__(self, cache_dir: Path, *, mode: str = "match",
                 titles: dict[str, str] | None = None):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.titles = titles or {}
        self.queries: list[str] = []
        self.errors: list[dict] = []
        self._seed = 0
        self._shared: Path | None = None

    def _record(self, query: str, path: Path) -> dict:
        data = path.read_bytes()
        with Image.open(io.BytesIO(data)) as photo:
            width, height = photo.size
        title = self.titles.get(query) or query
        if self.mode == "unrelated":
            title = "abstract blue gradient background texture"
        license_name = "CC0 1.0"
        license_url = "https://creativecommons.org/publicdomain/zero/1.0/"
        if self.mode == "disallowed_license":
            license_name = "CC BY-NC 4.0"
            license_url = "https://creativecommons.org/licenses/by-nc/4.0/"
        return {
            "provider": "fixture", "source_key": f"fixture:{hashlib.sha256(data).hexdigest()[:12]}",
            "title": title, "description": title, "creator": "Fixture artist",
            "source_page": "https://commons.wikimedia.org/wiki/File:fixture.png",
            "download_url": "https://images.example.org/fixture.png",
            "license": license_name, "license_version": "1.0",
            "license_url": license_url,
            "attribution": f"{title} by Fixture artist",
            "file": str(path), "sha256": hashlib.sha256(data).hexdigest(),
            "perceptual_hash": perceptual_hash(data), "bytes": len(data),
            "actual_format": "PNG", "actual_width": width, "actual_height": height,
        }

    def fetch_one(self, query, *, excluded_sha256=None, excluded_source_keys=None,
                  excluded_perceptual_hashes=None, scene_context=None, min_aspect=None):
        self.queries.append(query)
        if self.mode == "fail":
            self.errors.append({"query": query, "code": "SCENE.NO_LICENSED_PHOTO"})
            raise SceneImageError("SCENE.NO_LICENSED_PHOTO",
                                  "fixture provider has no licensed photo for this query")
        if self.mode == "provider_unavailable":
            self.errors.append({"query": query, "code": "SCENE.PROVIDER_UNAVAILABLE"})
            raise SceneImageError("SCENE.PROVIDER_UNAVAILABLE",
                                  "图片检索服务暂不可用（ServiceUnavailable）。")
        if self.mode == "reuse":
            if self._shared is None:
                self._shared = self.cache_dir / "shared.png"
                self._shared.write_bytes(png_bytes(1))
            return self._record(query, self._shared)
        if self.mode == "low_resolution":
            self.errors.append({"query": query, "code": "SCENE.IMAGE_RESOLUTION_TOO_LOW"})
            raise SceneImageError("SCENE.IMAGE_RESOLUTION_TOO_LOW",
                                  "图片分辨率不足以用作主要场景图。")
        if self.mode == "low_contrast":
            self.errors.append({"query": query, "code": "SCENE.PHOTO_CONTRAST_INSUFFICIENT"})
            raise SceneImageError("SCENE.PHOTO_CONTRAST_INSUFFICIENT",
                                  "下载素材对比度极低，缺乏可辨识细节与画面反差。")
        if self.mode == "flat_art":
            self.errors.append({"query": query, "code": "SCENE.PHOTO_NOT_PHOTOGRAPHIC"})
            raise SceneImageError("SCENE.PHOTO_NOT_PHOTOGRAPHIC",
                                  "下载素材是平涂插画/线描/矢量图而非场景照片。")
        self._seed += 1
        path = self.cache_dir / f"scene_{self._seed}.png"
        path.write_bytes(png_bytes(self._seed))
        return self._record(query, path)


def scene_entry(*, scene_key: str, canonical: str, topic: str, subject: str,
                action: str, setting: str, label: str, terms: dict,
                query: str, pages: list[tuple[int, str]], topic_zh: str = "",
                illustration_rationale: str = "") -> dict:
    """One open-vocabulary scene with verbatim page evidence."""
    return {
        "scene_key": scene_key, "canonical_scene_id": canonical,
        "topic": topic_zh or topic, "subject": subject, "action": action,
        "setting": setting, "label": label,
        "lighting": "natural daylight", "composition_camera": "medium shot",
        "style": "documentary photography", "quality": "high detail",
        "depiction_type": "illustrative",
        "illustration_rationale": illustration_rationale,
        "photo_search_terms": terms, "visual_query": query,
        "visual_query_alternatives": [],
        "page_candidates": [{"page": page, "relevance": .9, "evidence": [evidence]}
                            for page, evidence in pages],
    }


def resolution(scenes: list[dict], coverage: list[tuple[int, list[str]]]) -> dict:
    """Assemble a plan payload with a per-page evidence ledger.

    The page ledger must carry verbatim source evidence per page: the
    independent gate re-checks it against the original deck, so an empty
    ledger is a blocking finding rather than a formality.
    """
    by_page: dict[int, list[str]] = {}
    for scene in scenes:
        for candidate in scene["page_candidates"]:
            by_page.setdefault(int(candidate["page"]), []).extend(candidate["evidence"])
    rows = []
    for page, keys in coverage:
        evidence = list(dict.fromkeys(by_page.get(page, [])))
        rows.append({"page": page, "scene_keys": keys,
                     "no_scene_reason": None if keys or not evidence else "本页无具体场景",
                     "evidence": evidence})
    return {"schema_version": "2.0.0", "scenes": scenes, "page_coverage": rows}


EN_CONSUMER_PAGES = [
    ("Consumer basket", "Shoppers browse fresh produce along supermarket aisles."),
    ("Payment moment", "A cashier scans items at the checkout counter."),
    ("Proof of purchase", "A paper receipt and coins rest on a wooden desk."),
]


class MultilingualRecognitionAcceptance(unittest.TestCase):
    """Requirement 1: 26-language recognition and per-page language marking."""

    def test_all_26_scope_languages_are_recognised_from_original_text(self):
        samples = json.loads(FIXTURE.read_text(encoding="utf-8"))["languages"]
        self.assertEqual(set(samples), set(CURRENT_LANGUAGE_ACCEPTANCE_CODES))
        for code in CURRENT_LANGUAGE_ACCEPTANCE_CODES:
            with self.subTest(language=code):
                hint = detect_language_hint(samples[code])
                self.assertEqual(hint["primary"], code, hint)
                self.assertIn(code, hint["languages"])
                self.assertFalse(hint["mixed"], hint)
                self.assertFalse(hint["uncertain"], hint)

    def test_all_26_languages_are_marked_per_page_before_keyword_extraction(self):
        samples = json.loads(FIXTURE.read_text(encoding="utf-8"))["languages"]
        order = list(CURRENT_LANGUAGE_ACCEPTANCE_CODES)
        with tempfile.TemporaryDirectory() as tmp:
            source = make_deck(Path(tmp) / "26.pptx",
                               [(f"page {i}", samples[code])
                                for i, code in enumerate(order, 1)])
            plan = visual_plan.build_plan(str(source))

        self.assertEqual(len(plan["pages"]), 26)
        self.assertEqual(plan["language_support"]["detected_source_languages"], sorted(order))
        self.assertEqual(plan["keyword_scene_order"][1], "detect_language_and_script_offline")
        for index, code in enumerate(order):
            row = plan["pages"][index]
            with self.subTest(language=code):
                self.assertEqual(row["source_language"]["primary"], code)
                self.assertEqual(row["language_state"], "detected")
                self.assertEqual(row["source_language"]["detector"],
                                 "unicode-script-lexical-v4")
                if code == "zh":
                    self.assertEqual(row["legacy_keyword_matcher"], "ZH_ONLY_APPLIED")
                else:
                    # The Chinese fixed-tag library may not be applied to any
                    # other language, and it must not fabricate keywords.
                    self.assertEqual(row["legacy_keyword_matcher"],
                                     "BYPASSED_AFTER_LANGUAGE_DETECTION")
                    self.assertEqual(row["keywords"], [])
                    self.assertIsNone(row["image"])

    def test_mixed_language_page_is_marked_mixed_not_silently_chinese(self):
        chinese_with_english = (
            "本次发布会由 Apple 主办，主题是 iPhone 17 的 camera system 升级与 "
            "battery life 表现。")
        english_with_chinese = (
            "Quarterly retail sales grew while 消费者 confidence improved across the market.")
        pure_chinese = "消费者在超市购买食品，并比较价格。"
        hint = detect_language_hint(chinese_with_english)
        self.assertEqual(hint["primary"], "zh", hint)
        self.assertTrue(hint["mixed"], hint)
        self.assertEqual(set(hint["languages"]), {"zh", "en"}, hint)

        self.assertEqual(detect_language_hint(english_with_chinese)["primary"], "en")

        with tempfile.TemporaryDirectory() as tmp:
            source = make_deck(Path(tmp) / "mixed.pptx", [
                ("发布会", chinese_with_english),
                ("Quarterly", english_with_chinese),
                ("消费", pure_chinese),
            ])
            plan = visual_plan.build_plan(str(source))

        self.assertEqual([row["language_state"] for row in plan["pages"]],
                         ["mixed", "mixed", "detected"])
        self.assertEqual(plan["language_pending"]["mixed_pages"], [1, 2])
        self.assertEqual(plan["language_pending"]["uncertain_pages"], [])
        # The English-dominant page must not run the Chinese word library.
        self.assertEqual(plan["pages"][1]["legacy_keyword_matcher"],
                         "BYPASSED_AFTER_LANGUAGE_DETECTION")
        self.assertEqual(plan["language_support"]["mixed_language_pages"], [1, 2])

    def test_korean_hanja_is_not_reported_as_chinese_mixed_page(self):
        text = "고객이 商品을 구매하고 賣店에서 決濟한다. 매출이 증가했다."
        hint = detect_language_hint(text)
        self.assertEqual(hint["primary"], "ko", hint)
        self.assertEqual(hint["languages"], ["ko"], hint)
        self.assertFalse(hint["mixed"], hint)
        self.assertIn("Han", hint["scripts"])

    def test_proper_nouns_numbers_links_and_short_labels_stay_uncertain(self):
        cases = {
            "numbers": "2026 2025 2024 12.5% 3,200",
            "link": "https://example.com/a/b?c=1",
            "proper_nouns": "Tesla Model 3 Berlin Gigafactory",
            "short_label": "Q3 revenue",
            "emoji": "\U0001F680\U0001F680\U0001F680",
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                hint = detect_language_hint(text)
                self.assertEqual(hint["primary"], "und", hint)
                self.assertTrue(hint["uncertain"], hint)
                # A bare link must never be scored as Portuguese from ".com".
                self.assertNotIn("pt", hint["languages"], hint)

        with tempfile.TemporaryDirectory() as tmp:
            source = make_deck(Path(tmp) / "uncertain.pptx",
                               [(name, text) for name, text in cases.items()])
            plan = visual_plan.build_plan(str(source))

        self.assertEqual(plan["language_pending"]["uncertain_pages"], [1, 2, 3, 4, 5])
        for row in plan["pages"]:
            with self.subTest(page=row["page"]):
                self.assertEqual(row["language_state"], "uncertain")
                self.assertIsNone(row["image"])
                self.assertIn("待处理", row["reason"])
        self.assertEqual(plan["language_support"]["uncertain_language_pages"],
                         [1, 2, 3, 4, 5])


class NonChineseSemanticSceneAcceptance(unittest.TestCase):
    """Requirements 2 and 4: same semantic path, real placement, honest failure."""

    def _plan(self, tmp: str, pages, semantic, *, mode="match", titles=None):
        source = make_deck(Path(tmp) / "source.pptx", pages)
        provider = AcceptancePhotoProvider(Path(tmp) / "assets", mode=mode, titles=titles)
        plan = semantic_scenes.build_plan(
            source, semantic_resolution=semantic, provider=provider)
        return source, provider, plan

    def test_english_deck_plans_scenes_and_places_photos(self):
        semantic = resolution([
            scene_entry(scene_key="supermarket_shopping",
                        canonical="consumer_supermarket_shopping",
                        topic="Consumer retail", subject="shoppers",
                        action="browsing fresh produce", setting="supermarket aisles",
                        label="Supermarket shopping",
                        terms={"subject": "shoppers", "action": "browsing produce",
                               "setting": "supermarket"},
                        query="shoppers browsing produce supermarket",
                        pages=[(1, EN_CONSUMER_PAGES[0][1])]),
            scene_entry(scene_key="checkout_payment",
                        canonical="consumer_checkout_payment",
                        topic="Consumer retail", subject="cashier",
                        action="scanning items", setting="checkout counter",
                        label="Checkout payment",
                        terms={"subject": "cashier", "action": "scanning items",
                               "setting": "checkout"},
                        query="cashier scanning items checkout",
                        pages=[(2, EN_CONSUMER_PAGES[1][1])]),
            scene_entry(scene_key="receipt_still_life",
                        canonical="consumer_receipt_still_life",
                        topic="Consumer retail", subject="paper receipt",
                        action="resting on desk", setting="wooden desk",
                        label="Receipt still life",
                        terms={"subject": "receipt", "action": "resting desk",
                               "setting": "desk"},
                        query="receipt resting desk",
                        pages=[(3, EN_CONSUMER_PAGES[2][1])]),
        ], [(1, ["supermarket_shopping"]), (2, ["checkout_payment"]),
            (3, ["receipt_still_life"])])

        with tempfile.TemporaryDirectory() as tmp:
            source, provider, plan = self._plan(tmp, EN_CONSUMER_PAGES, semantic)
            self.assertEqual(plan["unresolved_scenes"], [])
            self.assertEqual(len(plan["images"]), 3)

            for spec in plan["images"].values():
                with self.subTest(scene=spec["scene_key"]):
                    # Neutral English query: no source-language characters and
                    # no digits are allowed to reach a photo provider.
                    self.assertRegex(spec["visual_query"], r"^[A-Za-z ]+$")
                    self.assertGreaterEqual(spec["area_ratio"], 0.12)
                    self.assertLessEqual(spec["text_overlap_ratio"], 0.28)
                    left, top, width, height = spec["rect"]
                    self.assertGreaterEqual(left, 0)
                    self.assertGreaterEqual(top, 0)
                    self.assertLessEqual(left + width, 13.334)
                    self.assertLessEqual(top + height, 7.501)

            # Evidence stays verbatim English source text, page by page.
            self.assertEqual(plan["images"]["supermarket_shopping"]["page"], 1)
            self.assertEqual(plan["images"]["supermarket_shopping"]["evidence"],
                             [EN_CONSUMER_PAGES[0][1]])
            self.assertEqual(plan["language_support"]["detected_source_languages"], ["en"])

            output = Path(tmp) / "out.pptx"
            result = semantic_scenes.apply_plan(source, plan, output)
            self.assertEqual(result["inserted"], 3)
            prs = Presentation(str(output))
            placed = [(page, shape) for page, slide in enumerate(prs.slides, 1)
                      for shape in slide.shapes
                      if shape.name.startswith("WENGAO_SCENE|")]
            self.assertEqual(len(placed), 3)
            self.assertEqual(sorted(page for page, _ in placed), [1, 2, 3])

            # The frozen plan and the inserted bytes must also pass the
            # independent scene gate, not just the planner's own bookkeeping.
            plan_path = Path(tmp) / "scene_plan.json"
            plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            gate = semantic_scenes.validate_output(source, output, plan_path,
                                                   Path(tmp) / "assets")
            self.assertEqual(gate["status"], "EXECUTED", gate["findings"])
            self.assertFalse(gate["findings"])

    def test_evidence_from_another_language_is_rejected(self):
        # An English page must not be "supported" by Chinese evidence: the
        # excerpt has to exist in that page's original text.
        semantic = resolution([
            scene_entry(scene_key="supermarket_shopping",
                        canonical="consumer_supermarket_shopping",
                        topic="Consumer retail", subject="shoppers",
                        action="browsing fresh produce", setting="supermarket aisles",
                        label="Supermarket shopping",
                        terms={"subject": "shoppers", "action": "browsing produce",
                               "setting": "supermarket"},
                        query="shoppers browsing produce supermarket",
                        pages=[(1, "消费者在超市推车选购生鲜食品")]),
        ], [(1, ["supermarket_shopping"]), (2, []), (3, [])])
        with tempfile.TemporaryDirectory() as tmp:
            source = make_deck(Path(tmp) / "source.pptx", EN_CONSUMER_PAGES)
            with self.assertRaises(semantic_scenes.SemanticSceneError) as ctx:
                semantic_scenes.build_plan(
                    source, semantic_resolution=semantic,
                    provider=AcceptancePhotoProvider(Path(tmp) / "assets"))
        self.assertEqual(ctx.exception.code, "SCENE.EVIDENCE_NOT_GROUNDED")

    def test_scene_prompt_carries_original_english_text_and_language_before_keywords(self):
        from wengao_ppt.scene_model import scene_prompt
        prompt = scene_prompt([{"page": 1, "title": "Consumer basket",
                                "text": EN_CONSUMER_PAGES[0][1]}])
        self.assertIn(EN_CONSUMER_PAGES[0][1], prompt)
        self.assertIn('"source_language_hint":"en"', prompt)
        self.assertIn("Before extracting any scene keywords", prompt)


class SceneDedupeAcceptance(unittest.TestCase):
    """Requirement 3: scene-level dedupe without collapsing distinct sub-scenes."""

    def _build(self, tmp, pages, semantic, mode="match"):
        source = make_deck(Path(tmp) / "source.pptx", pages)
        provider = AcceptancePhotoProvider(Path(tmp) / "assets", mode=mode)
        return semantic_scenes.build_plan(source, semantic_resolution=semantic,
                                          provider=provider)

    def test_same_scene_declared_twice_places_exactly_one_photo(self):
        # Two language variants of one concrete scene: one canonical identity,
        # one photo, both pages linked to the same scene.
        pages = [("消费", "消费者在超市推车选购生鲜食品。"),
                 ("Retail", "Shoppers browse fresh produce along supermarket aisles.")]
        semantic = resolution([
            scene_entry(scene_key="supermarket_shopping",
                        canonical="consumer_supermarket_shopping",
                        topic="消费零售", subject="消费者",
                        action="推车选购生鲜", setting="超市货架通道",
                        label="商超选购",
                        terms={"subject": "shoppers", "action": "browsing produce",
                               "setting": "supermarket"},
                        query="shoppers browsing produce supermarket",
                        pages=[(1, pages[0][1]), (2, pages[1][1])]),
        ], [(1, ["supermarket_shopping"]), (2, ["supermarket_shopping"])])
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._build(tmp, pages, semantic)
        self.assertEqual(len(plan["images"]), 1)
        self.assertEqual(len(plan["scenes"]), 1)
        self.assertEqual(plan["scenes"][0]["candidate_pages"], [1, 2])

    def test_canonical_id_separator_and_plural_variants_merge(self):
        pages = [("消费", "顾客在收银台结算扫码支付。"),
                 ("Retail", "A cashier scans items at the checkout counter.")]
        semantic = resolution([
            scene_entry(scene_key="checkout_a", canonical="consumer.checkout",
                        topic="消费零售", subject="顾客与收银员",
                        action="收银台结算", setting="超市收银台", label="结算",
                        terms={"subject": "cashier", "action": "scanning items",
                               "setting": "checkout"},
                        query="cashier scanning items checkout",
                        pages=[(1, pages[0][1])]),
            scene_entry(scene_key="checkout_b", canonical="consumer_checkouts",
                        topic="Consumer retail", subject="cashier",
                        action="scanning items", setting="checkout counter",
                        label="Checkout",
                        terms={"subject": "cashier", "action": "scanning items",
                               "setting": "checkout"},
                        query="cashier scanning items checkout",
                        pages=[(2, pages[1][1])]),
        ], [(1, ["checkout_a"]), (2, ["checkout_b"])])
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._build(tmp, pages, semantic)
        self.assertEqual(len(plan["scenes"]), 1)
        self.assertEqual(len(plan["images"]), 1)
        merged = [row for row in plan["dedupe_audit"] if row["decision"] == "merged"]
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["signature"], "canonical:consumer_checkout")

    def test_distinct_subscenes_of_one_topic_each_get_a_different_photo(self):
        semantic = resolution([
            scene_entry(scene_key="supermarket_shopping",
                        canonical="consumer_supermarket_shopping",
                        topic="Consumer retail", subject="shoppers",
                        action="browsing fresh produce", setting="supermarket aisles",
                        label="Supermarket shopping",
                        terms={"subject": "shoppers", "action": "browsing produce",
                               "setting": "supermarket"},
                        query="shoppers browsing produce supermarket",
                        pages=[(1, EN_CONSUMER_PAGES[0][1])]),
            scene_entry(scene_key="checkout_payment",
                        canonical="consumer_checkout_payment",
                        topic="Consumer retail", subject="cashier",
                        action="scanning items", setting="checkout counter",
                        label="Checkout payment",
                        terms={"subject": "cashier", "action": "scanning items",
                               "setting": "checkout"},
                        query="cashier scanning items checkout",
                        pages=[(2, EN_CONSUMER_PAGES[1][1])]),
            scene_entry(scene_key="receipt_still_life",
                        canonical="consumer_receipt_still_life",
                        topic="Consumer retail", subject="paper receipt",
                        action="resting on desk", setting="wooden desk",
                        label="Receipt still life",
                        terms={"subject": "receipt", "action": "resting desk",
                               "setting": "desk"},
                        query="receipt resting desk",
                        pages=[(3, EN_CONSUMER_PAGES[2][1])]),
        ], [(1, ["supermarket_shopping"]), (2, ["checkout_payment"]),
            (3, ["receipt_still_life"])])
        with tempfile.TemporaryDirectory() as tmp:
            plan = self._build(tmp, EN_CONSUMER_PAGES, semantic)
        self.assertEqual(plan["unresolved_scenes"], [])
        self.assertEqual(len(plan["images"]), 3)
        self.assertEqual(len({spec["sha256"] for spec in plan["images"].values()}), 3)
        self.assertEqual(sorted(plan["images"]), ["checkout_payment",
                                                  "receipt_still_life",
                                                  "supermarket_shopping"])
        self.assertTrue(all(row["decision"] == "kept" for row in plan["dedupe_audit"]))


class SceneFailureAcceptance(unittest.TestCase):
    """Requirement 4: failures are recorded, never dressed up as success."""

    def _build(self, tmp, semantic, mode):
        source = make_deck(Path(tmp) / "source.pptx", EN_CONSUMER_PAGES[:1])
        provider = AcceptancePhotoProvider(Path(tmp) / "assets", mode=mode)
        plan = semantic_scenes.build_plan(source, semantic_resolution=semantic,
                                          provider=provider)
        return source, plan

    def _semantic(self):
        return resolution([
            scene_entry(scene_key="supermarket_shopping",
                        canonical="consumer_supermarket_shopping",
                        topic="Consumer retail", subject="shoppers",
                        action="browsing fresh produce", setting="supermarket aisles",
                        label="Supermarket shopping",
                        terms={"subject": "shoppers", "action": "browsing produce",
                               "setting": "supermarket"},
                        query="shoppers browsing produce supermarket",
                        pages=[(1, EN_CONSUMER_PAGES[0][1])]),
        ], [(1, ["supermarket_shopping"])])

    def test_retrieval_failure_is_recorded_and_blocks_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, plan = self._build(tmp, self._semantic(), "fail")
            self.assertEqual(plan["images"], {})
            self.assertEqual(len(plan["unresolved_scenes"]), 1)
            self.assertEqual(plan["unresolved_scenes"][0]["reason"],
                             "SCENE.NO_LICENSED_PHOTO")
            # No text-card or placeholder may be recorded as a scene image.
            serialized = json.dumps(plan, ensure_ascii=False)
            self.assertNotIn("text_card", serialized)
            self.assertNotIn("placeholder_image", serialized)
            with self.assertRaises(semantic_scenes.SemanticSceneError) as ctx:
                semantic_scenes.apply_plan(source, plan, Path(tmp) / "out.pptx")
        self.assertEqual(ctx.exception.code, "SCENE.UNRESOLVED_SCENES")

    def test_unrelated_photo_is_rejected_by_metadata_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, plan = self._build(tmp, self._semantic(), "unrelated")
        self.assertEqual(plan["images"], {})
        self.assertEqual(len(plan["unresolved_scenes"]), 1)
        self.assertIn(plan["unresolved_scenes"][0]["reason"],
                      {"SCENE.IMAGE_CONTEXT_UNVERIFIED",
                       "SCENE.IMAGE_RELEVANCE_UNVERIFIED"})

    def test_one_photo_cannot_be_reused_for_two_scenes(self):
        pages = [("Consumer basket", EN_CONSUMER_PAGES[0][1]),
                 ("Payment moment", EN_CONSUMER_PAGES[1][1])]
        semantic = resolution([
            scene_entry(scene_key="supermarket_shopping",
                        canonical="consumer_supermarket_shopping",
                        topic="Consumer retail", subject="shoppers",
                        action="browsing fresh produce", setting="supermarket aisles",
                        label="Supermarket shopping",
                        terms={"subject": "shoppers", "action": "browsing produce",
                               "setting": "supermarket"},
                        query="shoppers browsing produce supermarket",
                        pages=[(1, pages[0][1])]),
            scene_entry(scene_key="checkout_payment",
                        canonical="consumer_checkout_payment",
                        topic="Consumer retail", subject="cashier",
                        action="scanning items", setting="checkout counter",
                        label="Checkout payment",
                        terms={"subject": "cashier", "action": "scanning items",
                               "setting": "checkout"},
                        query="cashier scanning items checkout",
                        pages=[(2, pages[1][1])]),
        ], [(1, ["supermarket_shopping"]), (2, ["checkout_payment"])])
        with tempfile.TemporaryDirectory() as tmp:
            source = make_deck(Path(tmp) / "source.pptx", pages)
            provider = AcceptancePhotoProvider(Path(tmp) / "assets", mode="reuse")
            plan = semantic_scenes.build_plan(source, semantic_resolution=semantic,
                                              provider=provider)
        self.assertEqual(len(plan["images"]), 1)
        self.assertEqual(len(plan["unresolved_scenes"]), 1)
        self.assertIn(plan["unresolved_scenes"][0]["reason"],
                      {"SCENE.PHOTO_REUSED_OR_UNVERIFIED",
                       "SCENE.NEAR_DUPLICATE_PHOTO"})



class Multilingual26AllPositiveAcceptance(unittest.TestCase):
    """Fixture-injected scene insertion path coverage for all 26 languages.

    This deterministic test injects a source-aligned semantic record. It checks
    original-language anchors, canonical identity, neutral English photo query,
    editable OOXML insertion, and the independent scene gate. It does NOT prove
    that a real model independently understood all 26 languages; that remains
    a separate real-model end-to-end acceptance gate.
    """

    # Each tuple is (canonical scene, source-language subject, action, setting).
    # Several languages intentionally share one canonical consumer or clinic
    # scene to exercise identity reuse without replacing semantic fields with a
    # generic English worker/workspace fixture.
    SCENE_CASES = {
        "zh": ("consumer_supermarket_shopping", "消费者", "购买食品", "超市"),
        "en": ("consumer_supermarket_shopping", "Consumers", "shop", "supermarkets"),
        "hi": ("consumer_supermarket_shopping", "उपभोक्ता", "भोजन खरीदते हैं", "सुपरमार्केट"),
        "es": ("consumer_supermarket_shopping", "consumidores", "compran alimentos", "supermercado"),
        "ar": ("consumer_supermarket_shopping", "المستهلكون", "يشتري", "المتجر"),
        "fr": ("consumer_supermarket_shopping", "consommateurs", "achètent des produits", "supermarché"),
        "bn": ("consumer_supermarket_shopping", "ক্রেতারা", "খাবার কেনেন", "সুপারমার্কেটে"),
        "pt": ("consumer_supermarket_shopping", "consumidores", "compram alimentos", "supermercado"),
        "ru": ("consumer_supermarket_shopping", "Потребители", "покупают продукты", "супермаркете"),
        "ur": ("consumer_supermarket_shopping", "صارفین", "خوراک خریدتے ہیں", "سپر مارکیٹ"),
        "id": ("consumer_supermarket_shopping", "konsumen", "membeli makanan", "supermarket"),
        "de": ("consumer_supermarket_shopping", "Verbraucher", "kaufen Lebensmittel", "Supermarkt"),
        "ja": ("consumer_supermarket_shopping", "消費者", "食品を購入", "スーパーマーケット"),
        "te": ("consumer_supermarket_shopping", "వినియోగదారులు", "ఆహారం కొనుగోలు", "సూపర్‌మార్కెట్"),
        "tr": ("healthcare_doctor_consultation", "hasta", "doktor ile konuşur", "klinikte"),
        "ta": ("consumer_supermarket_shopping", "நுகர்வோர்", "உணவு வாங்கி", "பல்பொருள் அங்காடியில்"),
        "vi": ("healthcare_doctor_consultation", "Người bệnh", "nói với bác sĩ", "phòng khám"),
        "ko": ("consumer_supermarket_shopping", "소비자", "식품을 구매하고", "슈퍼마켓에서"),
        "fa": ("healthcare_doctor_consultation", "پزشک", "گفتگو می‌کند", "درمانگاه"),
        "sw": ("healthcare_doctor_consultation", "Mgonjwa", "anazungumza", "kliniki"),
        "it": ("consumer_supermarket_shopping", "utenti", "acquistano cibo", "supermercato"),
        "th": ("consumer_supermarket_shopping", "ผู้บริโภค", "ซื้ออาหาร", "ซูเปอร์มาร์เก็ต"),
        "pl": ("healthcare_doctor_consultation", "Pacjent", "rozmawia z lekarzem o leczeniu", "przychodni"),
        "uk": ("consumer_supermarket_shopping", "Споживачі", "купують продукти", "супермаркеті"),
        "ms": ("consumer_supermarket_shopping", "Pengguna", "membeli barangan", "pasar raya"),
        "nl": ("consumer_supermarket_shopping", "gebruikers", "vergelijken aanbiedingen", "supermarkt"),
    }

    def test_all_26_languages_have_positive_source_grounded_scene_integration(self):
        samples = json.loads(FIXTURE.read_text(encoding="utf-8"))["languages"]
        self.assertEqual(set(self.SCENE_CASES), set(CURRENT_LANGUAGE_ACCEPTANCE_CODES))
        for code in CURRENT_LANGUAGE_ACCEPTANCE_CODES:
            with self.subTest(language=code):
                sample_text = samples[code]
                canonical_id, subject, action, setting = self.SCENE_CASES[code]
                # Require the injected scene to use source-language anchors
                # that actually occur in this page, rather than a generic
                # English scene unrelated to the sample's meaning.
                for anchor in (subject, action, setting):
                    self.assertIn(anchor.casefold(), sample_text.casefold(),
                                  f"{code} fixture anchor is not grounded: {anchor}")
                query = ("doctor consulting patient clinic"
                         if canonical_id == "healthcare_doctor_consultation"
                         else "shoppers buying groceries supermarket")
                query_terms = (("doctor", "consulting patient", "clinic")
                               if canonical_id == "healthcare_doctor_consultation"
                               else ("shoppers", "buying groceries", "supermarket"))
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    source_deck = make_deck(tmp_path / f"deck_{code}.pptx",
                                            [(f"{code.upper()} Topic", sample_text)])
                    # 1. Verify language detection precedes keyword/scene extraction
                    hint = detect_language_hint(sample_text)
                    self.assertEqual(hint["primary"], code)

                    # 2. Open-vocabulary scene resolution grounded in source text
                    scene_key = f"scene_{code}"
                    semantic = resolution([
                        scene_entry(
                            scene_key=scene_key,
                            canonical=canonical_id,
                            topic=subject,
                            subject=subject,
                            action=action,
                            setting=setting,
                            label=subject,
                            terms={"subject": query_terms[0],
                                   "action": query_terms[1],
                                   "setting": query_terms[2]},
                            query=query,
                            pages=[(1, sample_text)],
                            illustration_rationale=sample_text,
                        ),
                    ], [(1, [scene_key])])

                    provider = AcceptancePhotoProvider(tmp_path / "assets")
                    plan = semantic_scenes.build_plan(source_deck, semantic_resolution=semantic,
                                                      provider=provider)
                    self.assertEqual(plan["unresolved_scenes"], [])
                    self.assertEqual(len(plan["images"]), 1)
                    spec = plan["images"][scene_key]

                    # 3. Verify per-image provenance recorded in plan spec
                    self.assertEqual(spec["page"], 1)
                    self.assertEqual(spec["visual_query"], query)
                    self.assertEqual(spec["query"], query)
                    self.assertTrue(spec["source_page"].startswith("https://"))
                    self.assertEqual(spec["provider"], "fixture")
                    self.assertTrue(spec["license"].startswith("CC0"))
                    self.assertTrue(spec["download_url"].startswith("https://"))
                    self.assertTrue(bool(spec["asset_id"]))
                    self.assertEqual(spec["content_hash"], spec["sha256"])

                    # 4. Insert image into PPTX
                    out_pptx = tmp_path / f"out_{code}.pptx"
                    apply_result = semantic_scenes.apply_plan(source_deck, plan, out_pptx)
                    self.assertEqual(apply_result["inserted"], 1)
                    action = apply_result["actions"][0]

                    # 5. Verify every required field is present in action record
                    self.assertEqual(action["scene_key"], scene_key)
                    self.assertEqual(action["page"], 1)
                    self.assertEqual(action["sha256"], spec["sha256"])
                    self.assertEqual(action["content_hash"], spec["sha256"])
                    self.assertEqual(action["query"], spec["query"])
                    self.assertEqual(action["visual_query"], spec["visual_query"])
                    self.assertEqual(action["source_page"], spec["source_page"])
                    self.assertEqual(action["provider"], spec["provider"])
                    self.assertEqual(action["license"], spec["license"])
                    self.assertEqual(action["download_url"], spec["download_url"])
                    self.assertEqual(action["asset_id"], spec["asset_id"])
                    self.assertIsInstance(action["shape_id"], int)
                    self.assertTrue(action["shape_name"].startswith("WENGAO_SCENE|"))
                    self.assertEqual(action["object_id"], action["shape_id"])
                    self.assertEqual(action["object_name"], action["shape_name"])

                    # 6. Verify image is in slide shapes as an editable MSO_SHAPE_TYPE.PICTURE
                    prs = Presentation(str(out_pptx))
                    slide = prs.slides[0]
                    placed_shapes = [s for s in slide.shapes
                                     if getattr(s, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE
                                     and s.name.startswith("WENGAO_SCENE|")]
                    self.assertEqual(len(placed_shapes), 1)
                    self.assertEqual(placed_shapes[0].shape_id, action["shape_id"])

                    # 7. Independent scene gate validation
                    plan_path = tmp_path / f"plan_{code}.json"
                    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
                    gate = semantic_scenes.validate_output(source_deck, out_pptx, plan_path)
                    self.assertEqual(gate["status"], "EXECUTED", gate["findings"])


class CrossThemePositiveAcceptance(unittest.TestCase):
    """Positive acceptance verifying semantic scene placement across all supported themes."""

    def test_all_registered_themes_preserve_semantic_scenes_and_pass_gate(self):
        from wengao_ppt.theme import THEMES, apply_theme
        registered_themes = sorted(THEMES.keys())
        self.assertGreaterEqual(len(registered_themes), 5)

        sample_pages = [("Modern Retail", EN_CONSUMER_PAGES[0][1])]
        semantic = resolution([
            scene_entry(scene_key="supermarket_shopping",
                        canonical="consumer_supermarket_shopping",
                        topic="Consumer retail", subject="shoppers",
                        action="browsing fresh produce", setting="supermarket aisles",
                        label="Supermarket shopping",
                        terms={"subject": "shoppers", "action": "browsing produce",
                               "setting": "supermarket"},
                        query="shoppers browsing produce supermarket",
                        pages=[(1, sample_pages[0][1])]),
        ], [(1, ["supermarket_shopping"])])

        for theme_id in registered_themes:
            with self.subTest(theme=theme_id):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    source = make_deck(tmp_path / "raw.pptx", sample_pages)
                    themed_source = tmp_path / f"themed_{theme_id}.pptx"
                    apply_theme(source, themed_source, theme_name=theme_id)

                    provider = AcceptancePhotoProvider(tmp_path / "assets")
                    plan = semantic_scenes.build_plan(themed_source, semantic_resolution=semantic,
                                                      provider=provider)
                    self.assertEqual(len(plan["images"]), 1)

                    output_pptx = tmp_path / f"final_{theme_id}.pptx"
                    res = semantic_scenes.apply_plan(themed_source, plan, output_pptx)
                    self.assertEqual(res["inserted"], 1)

                    plan_path = tmp_path / "plan.json"
                    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
                    gate = semantic_scenes.validate_output(themed_source, output_pptx, plan_path)
                    self.assertEqual(gate["status"], "EXECUTED", gate["findings"])


class SubsceneDeduplicationAndDiversityAcceptance(unittest.TestCase):
    """Verifies canonical scene deduplication vs distinct subscene separation."""

    def test_identical_canonical_scene_across_deck_embeds_only_one_photo(self):
        # 3 slides describing the identical shopping scene
        pages = [
            ("Store Entrance", "Shoppers browse fresh produce along supermarket aisles."),
            ("Produce Area", "Shoppers browse fresh produce along supermarket aisles in the morning."),
            ("Fresh Section", "Shoppers browse fresh produce along supermarket aisles daily."),
        ]
        semantic = resolution([
            scene_entry(scene_key="supermarket_shopping",
                        canonical="consumer_supermarket_shopping",
                        topic="Retail", subject="shoppers",
                        action="browsing produce", setting="supermarket",
                        label="Supermarket shopping",
                        terms={"subject": "shoppers", "action": "browsing produce",
                               "setting": "supermarket"},
                        query="shoppers browsing produce supermarket",
                        pages=[(1, pages[0][1]), (2, pages[1][1]), (3, pages[2][1])]),
        ], [(1, ["supermarket_shopping"]), (2, ["supermarket_shopping"]),
            (3, ["supermarket_shopping"])])

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = make_deck(tmp_path / "source.pptx", pages)
            provider = AcceptancePhotoProvider(tmp_path / "assets")
            plan = semantic_scenes.build_plan(source, semantic_resolution=semantic,
                                              provider=provider)
            # Exactly 1 image planned for the entire deck
            self.assertEqual(len(plan["images"]), 1)
            output = tmp_path / "out.pptx"
            res = semantic_scenes.apply_plan(source, plan, output)
            self.assertEqual(res["inserted"], 1)

            prs = Presentation(str(output))
            scene_shapes = [s for slide in prs.slides for s in slide.shapes
                            if s.name.startswith("WENGAO_SCENE|")]
            self.assertEqual(len(scene_shapes), 1)

            plan_path = tmp_path / "plan.json"
            plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            gate = semantic_scenes.validate_output(source, output, plan_path)
            self.assertEqual(gate["status"], "EXECUTED", gate["findings"])

    def test_distinct_subscenes_within_one_topic_remain_distinct_and_placed(self):
        # Healthcare topic with 3 clearly distinct subscenes
        pages = [
            ("Outpatient Clinic", "A doctor consults an elderly patient inside the examination clinic."),
            ("Operating Room", "Surgeons perform laparoscopic surgery in the sterile operating room."),
            ("Hospital Pharmacy", "A pharmacist dispenses prescription medicine behind the pharmacy counter."),
        ]
        semantic = resolution([
            scene_entry(scene_key="clinic_doctor_consultation",
                        canonical="healthcare_clinic_consultation",
                        topic="Healthcare", subject="doctor",
                        action="consulting patient", setting="clinic office",
                        label="Doctor consultation",
                        terms={"subject": "doctor", "action": "consulting patient", "setting": "clinic"},
                        query="doctor consulting patient clinic",
                        pages=[(1, pages[0][1])]),
            scene_entry(scene_key="operating_theater_surgery",
                        canonical="healthcare_operating_theater_surgery",
                        topic="Healthcare", subject="surgeons",
                        action="performing surgery", setting="operating theater",
                        label="Surgical operation",
                        terms={"subject": "surgeons", "action": "performing surgery", "setting": "operating theater"},
                        query="surgeons performing surgery operating theater",
                        pages=[(2, pages[1][1])]),
            scene_entry(scene_key="pharmacy_dispensing",
                        canonical="healthcare_pharmacy_dispensing",
                        topic="Healthcare", subject="pharmacist",
                        action="dispensing medicine", setting="pharmacy counter",
                        label="Pharmacy dispensing",
                        terms={"subject": "pharmacist", "action": "dispensing medicine", "setting": "pharmacy counter"},
                        query="pharmacist dispensing medicine pharmacy counter",
                        pages=[(3, pages[2][1])]),
        ], [(1, ["clinic_doctor_consultation"]), (2, ["operating_theater_surgery"]),
            (3, ["pharmacy_dispensing"])])

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = make_deck(tmp_path / "source.pptx", pages)
            provider = AcceptancePhotoProvider(tmp_path / "assets")
            plan = semantic_scenes.build_plan(source, semantic_resolution=semantic,
                                              provider=provider)
            # All 3 subscenes planned separately
            self.assertEqual(len(plan["images"]), 3)
            self.assertEqual(plan["unresolved_scenes"], [])

            output = tmp_path / "out.pptx"
            res = semantic_scenes.apply_plan(source, plan, output)
            self.assertEqual(res["inserted"], 3)

            # Ensure 3 distinct files, distinct hashes, distinct queries
            sha_set = {spec["sha256"] for spec in plan["images"].values()}
            query_set = {spec["visual_query"] for spec in plan["images"].values()}
            self.assertEqual(len(sha_set), 3)
            self.assertEqual(len(query_set), 3)

            prs = Presentation(str(output))
            scene_shapes = [s for slide in prs.slides for s in slide.shapes
                            if s.name.startswith("WENGAO_SCENE|")]
            self.assertEqual(len(scene_shapes), 3)

            plan_path = tmp_path / "plan.json"
            plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            gate = semantic_scenes.validate_output(source, output, plan_path)
            self.assertEqual(gate["status"], "EXECUTED", gate["findings"])


class ExtendedFailClosedNegativeAcceptance(unittest.TestCase):
    """Negative acceptance verifying fail-closed behavior on all invalid conditions."""

    def _build_plan(self, tmp_path, mode):
        pages = [("Consumer Retail", EN_CONSUMER_PAGES[0][1])]
        semantic = resolution([
            scene_entry(scene_key="supermarket_shopping",
                        canonical="consumer_supermarket_shopping",
                        topic="Consumer retail", subject="shoppers",
                        action="browsing fresh produce", setting="supermarket aisles",
                        label="Supermarket shopping",
                        terms={"subject": "shoppers", "action": "browsing produce",
                               "setting": "supermarket"},
                        query="shoppers browsing produce supermarket",
                        pages=[(1, pages[0][1])]),
        ], [(1, ["supermarket_shopping"])])
        source = make_deck(tmp_path / "source.pptx", pages)
        provider = AcceptancePhotoProvider(tmp_path / "assets", mode=mode)
        plan = semantic_scenes.build_plan(source, semantic_resolution=semantic,
                                          provider=provider)
        return source, plan

    def test_fail_closed_when_provider_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source, plan = self._build_plan(tmp_path, "provider_unavailable")
            self.assertEqual(len(plan["images"]), 0)
            self.assertEqual(len(plan["unresolved_scenes"]), 1)
            self.assertEqual(plan["unresolved_scenes"][0]["reason"], "SCENE.PROVIDER_UNAVAILABLE")
            with self.assertRaises(semantic_scenes.SemanticSceneError) as ctx:
                semantic_scenes.apply_plan(source, plan, tmp_path / "out.pptx")
            self.assertEqual(ctx.exception.code, "SCENE.UNRESOLVED_SCENES")

    def test_fail_closed_on_disallowed_license(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source, plan = self._build_plan(tmp_path, "disallowed_license")
            self.assertEqual(len(plan["images"]), 0)
            self.assertEqual(len(plan["unresolved_scenes"]), 1)
            self.assertIn(plan["unresolved_scenes"][0]["reason"],
                          {"SCENE.LICENSE_METADATA_MISSING", "SCENE.LICENSE_NOT_ALLOWED"})
            with self.assertRaises(semantic_scenes.SemanticSceneError) as ctx:
                semantic_scenes.apply_plan(source, plan, tmp_path / "out.pptx")
            self.assertEqual(ctx.exception.code, "SCENE.UNRESOLVED_SCENES")

    def test_fail_closed_on_flat_art_illustration(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source, plan = self._build_plan(tmp_path, "flat_art")
            self.assertEqual(len(plan["images"]), 0)
            self.assertEqual(len(plan["unresolved_scenes"]), 1)
            self.assertEqual(plan["unresolved_scenes"][0]["reason"], "SCENE.PHOTO_NOT_PHOTOGRAPHIC")
            with self.assertRaises(semantic_scenes.SemanticSceneError) as ctx:
                semantic_scenes.apply_plan(source, plan, tmp_path / "out.pptx")
            self.assertEqual(ctx.exception.code, "SCENE.UNRESOLVED_SCENES")

    def test_fail_closed_on_low_resolution_photo(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source, plan = self._build_plan(tmp_path, "low_resolution")
            # 400x250 is smaller than 600x360
            self.assertEqual(len(plan["images"]), 0)
            self.assertEqual(len(plan["unresolved_scenes"]), 1)
            self.assertEqual(plan["unresolved_scenes"][0]["reason"], "SCENE.IMAGE_RESOLUTION_TOO_LOW")
            with self.assertRaises(semantic_scenes.SemanticSceneError) as ctx:
                semantic_scenes.apply_plan(source, plan, tmp_path / "out.pptx")
            self.assertEqual(ctx.exception.code, "SCENE.UNRESOLVED_SCENES")

    def test_fail_closed_on_insufficient_contrast_photo(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source, plan = self._build_plan(tmp_path, "low_contrast")
            self.assertEqual(len(plan["images"]), 0)
            self.assertEqual(len(plan["unresolved_scenes"]), 1)
            self.assertEqual(plan["unresolved_scenes"][0]["reason"], "SCENE.PHOTO_CONTRAST_INSUFFICIENT")
            with self.assertRaises(semantic_scenes.SemanticSceneError) as ctx:
                semantic_scenes.apply_plan(source, plan, tmp_path / "out.pptx")
            self.assertEqual(ctx.exception.code, "SCENE.UNRESOLVED_SCENES")

    def test_fail_closed_on_object_insertion_failure(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source, plan = self._build_plan(tmp_path, "match")
            self.assertEqual(len(plan["images"]), 1)
            # 1. Identity change check
            spec = plan["images"]["supermarket_shopping"]
            orig_file = spec["file"]
            spec["file"] = str(tmp_path / "nonexistent.png")
            with self.assertRaises(semantic_scenes.SemanticSceneError) as ctx:
                semantic_scenes.apply_plan(source, plan, tmp_path / "out1.pptx")
            self.assertEqual(ctx.exception.code, "SCENE.PHOTO_IDENTITY_CHANGED")

            # 2. Add_picture failure check -> SCENE.OBJECT_INSERTION_FAILED
            spec["file"] = orig_file
            with mock.patch("pptx.shapes.shapetree.SlideShapes.add_picture",
                            side_effect=RuntimeError("OOXML shape tree allocation failed")):
                with self.assertRaises(semantic_scenes.SemanticSceneError) as ctx2:
                    semantic_scenes.apply_plan(source, plan, tmp_path / "out2.pptx")
                self.assertEqual(ctx2.exception.code, "SCENE.OBJECT_INSERTION_FAILED")


class RealPptxRenderAcceptance(unittest.TestCase):
    """Proves scene images enter editable PPTX shapes and appear in actual rendered pixels."""

    def test_real_pptx_scene_image_in_editable_shape_and_rendered_pixels(self):
        from wengao_ppt.render import render_real, real_render_available
        if not real_render_available():
            self.skipTest("LibreOffice real renderer not available on host")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source = make_deck(tmp_path / "source.pptx", [
                ("Retail Market", "Shoppers browse fresh produce along supermarket aisles."),
            ])
            semantic = resolution([
                scene_entry(scene_key="supermarket_shopping",
                            canonical="consumer_supermarket_shopping",
                            topic="Retail", subject="shoppers",
                            action="browsing produce", setting="supermarket",
                            label="Supermarket shopping",
                            terms={"subject": "shoppers", "action": "browsing produce", "setting": "supermarket"},
                            query="shoppers browsing produce supermarket",
                            pages=[(1, "Shoppers browse fresh produce along supermarket aisles.")]),
            ], [(1, ["supermarket_shopping"])])

            provider = AcceptancePhotoProvider(tmp_path / "assets")
            plan = semantic_scenes.build_plan(source, semantic_resolution=semantic, provider=provider)
            cand_pptx = tmp_path / "candidate.pptx"
            apply_res = semantic_scenes.apply_plan(source, plan, cand_pptx)
            self.assertEqual(apply_res["inserted"], 1)

            # 1. Verify photo is an editable shape in PPTX
            prs = Presentation(str(cand_pptx))
            slide = prs.slides[0]
            scene_shapes = [s for s in slide.shapes
                            if getattr(s, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE
                            and s.name.startswith("WENGAO_SCENE|")]
            self.assertEqual(len(scene_shapes), 1)
            self.assertEqual(scene_shapes[0].shape_id, apply_res["actions"][0]["shape_id"])

            plan_path = tmp_path / "plan.json"
            plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

            # 2. Make suppressed reference PPTX
            supp_pptx = tmp_path / "suppressed.pptx"
            semantic_scenes.make_scene_suppressed_pptx(cand_pptx, supp_pptx)

            # 3. Real LibreOffice rendering
            cand_render = render_real(cand_pptx, tmp_path / "cand_out")
            supp_render = render_real(supp_pptx, tmp_path / "supp_out")
            self.assertEqual(cand_render.get("exit_code"), 0)
            self.assertEqual(supp_render.get("exit_code"), 0)

            cand_pdf = Path(cand_render["pdf"])
            supp_pdf = Path(supp_render["pdf"])
            cand_png = Path(cand_render["png_bindings"][0]["file"])
            supp_png = Path(supp_render["png_bindings"][0]["file"])

            evidence = {
                "schema": "WG-SEMANTIC-SCENE-RENDER-EVIDENCE/1",
                "candidate_pptx_sha256": semantic_scenes._sha(cand_pptx),
                "scene_plan_sha256": plan["plan_sha256"],
                "scene_suppressed_pptx_sha256": semantic_scenes._sha(supp_pptx),
                "policy_sha256": semantic_scenes.POLICY_SHA256,
                "rendered_pdf_sha256": semantic_scenes._sha(cand_pdf),
                "rendered_pdf_path": str(cand_pdf),
                "suppressed_rendered_pdf_sha256": semantic_scenes._sha(supp_pdf),
                "suppressed_rendered_pdf_path": str(supp_pdf),
                "renderer": {"name": "LibreOffice", "version": "26.8.1.1"},
                "pages": [{
                    "page": 1,
                    "png_path": str(cand_png),
                    "png_sha256": semantic_scenes._sha(cand_png),
                    "suppressed_png_path": str(supp_png),
                    "suppressed_png_sha256": semantic_scenes._sha(supp_png),
                }],
            }

            # 4. Pixel visibility gate
            visibility = semantic_scenes.validate_rendered_scene_visibility(
                cand_pptx, plan_path, evidence)
            self.assertEqual(visibility["status"], "EXECUTED", visibility["findings"])
            self.assertFalse(visibility["blocking"])
            self.assertEqual(visibility["findings"], [])
            scene_summary = visibility["summary"]["scenes"]["supermarket_shopping"]
            self.assertTrue(scene_summary["visible"])
            self.assertGreaterEqual(scene_summary["matched_tile_fraction"], 0.65)


if __name__ == "__main__":
    unittest.main()
