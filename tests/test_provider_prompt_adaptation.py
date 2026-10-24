"""Unit tests for provider-aware prompt adaptation and scene lighting resolution.

Tests adhere to hard-gate.md specifications:
- Structured 7-field prompt building
- Negative prompt delivery only to providers explicitly supporting it
- Scene-specific lighting conflict resolution (natural light never overwrites night/industrial/hospital)
- Concise 2-4 word neutral English search queries
- Full 22-field image plan schema
"""
import unittest

from wengao_ppt.scene_images import (
    DEFAULT_NEGATIVE_PROMPT_EN,
    PROVIDER_REGISTRY,
    adapt_prompt_for_provider,
    build_image_plan,
    build_positive_prompt,
    build_provider_search_query,
    resolve_scene_lighting,
)


class TestProviderPromptAdaptation(unittest.TestCase):

    def test_lighting_conflict_resolution_night(self):
        # Night scene with user preference containing "自然光"
        lighting = resolve_scene_lighting(
            environment="早高峰城市立交桥与夜晚集市",
            action_state="夜晚巡检管线设施，手电照明",
            preferred_lighting="自然光摄影",
        )
        self.assertNotIn("自然光", lighting)
        self.assertIn("night", lighting.lower())

    def test_lighting_conflict_resolution_hospital(self):
        # Hospital CT/MRI imaging room
        lighting = resolve_scene_lighting(
            environment="医院放射科阅片室",
            action_state="医生审阅CT影像",
            preferred_lighting="自然光",
        )
        self.assertNotIn("自然光", lighting)
        self.assertIn("hospital", lighting.lower())

    def test_lighting_conflict_resolution_industrial_welding(self):
        # Factory welding workshop
        lighting = resolve_scene_lighting(
            environment="汽车总装车间",
            action_state="机械臂焊接作业，火花四溅",
            preferred_lighting="自然光",
        )
        self.assertNotIn("自然光", lighting)
        self.assertIn("spark", lighting.lower())

    def test_lighting_conflict_resolution_server_room(self):
        # Server room / data center
        lighting = resolve_scene_lighting(
            environment="机房服务器机柜",
            action_state="运维人员排查网络故障，指示灯闪烁",
            preferred_lighting="自然光",
        )
        self.assertNotIn("自然光", lighting)
        self.assertIn("blue led", lighting.lower())

    def test_lighting_natural_sunlight_outdoors(self):
        # Outdoor park trail
        lighting = resolve_scene_lighting(
            environment="社区公园林荫步道",
            action_state="老人与家人散步",
            preferred_lighting=None,
        )
        self.assertIn("sunlight", lighting.lower())

    def test_provider_search_query_generation(self):
        query = build_provider_search_query(
            subject="radiologist",
            environment="hospital imaging room",
            action_state="reviewing computed tomography scan",
            max_words=4,
        )
        words = query.split()
        self.assertTrue(2 <= len(words) <= 4, f"Query words length {len(words)} not in [2, 4]: '{query}'")
        self.assertNotIn("8k", query.lower())
        self.assertNotIn("realistic", query.lower())
        self.assertNotIn("blurry", query.lower())

    def test_adapt_prompt_for_photo_search_provider(self):
        raw_plan = {
            "subject": "factory worker",
            "environment": "assembly line",
            "action_state": "inspecting electronics component",
            "lighting": "industrial overhead lighting",
            "composition_camera": "medium close-up",
            "style": "documentary photography",
            "quality": "sharp focus, high resolution",
            "provider_query": "factory worker assembly line",
        }
        # Wikimedia Commons: photo search provider
        adapted = adapt_prompt_for_provider(raw_plan, "wikimedia-commons")
        self.assertEqual(adapted["provider_mode"], "photo_search")
        self.assertIsNone(adapted["negative_prompt"])
        self.assertNotIn("8k", adapted["provider_query"].lower())
        self.assertTrue(adapted["provider_query"])

    def test_adapt_prompt_for_t2i_with_negative_support(self):
        raw_plan = {
            "subject": "doctor",
            "environment": "clinic room",
            "action_state": "consulting patient",
            "lighting": "clean indoor lighting",
            "composition_camera": "eye-level medium shot",
            "style": "realistic photo",
            "quality": "sharp details",
        }
        # Stable Diffusion: supports negative prompt
        adapted = adapt_prompt_for_provider(raw_plan, "stable_diffusion")
        self.assertEqual(adapted["provider_mode"], "text_to_image")
        self.assertIsNotNone(adapted["negative_prompt"])
        self.assertIn("blurry", adapted["negative_prompt"].lower())
        self.assertIn("doctor", adapted["positive_prompt"].lower())

    def test_adapt_prompt_for_t2i_without_negative_support(self):
        raw_plan = {
            "subject": "office worker",
            "environment": "modern desk",
            "action_state": "typing on laptop",
            "lighting": "office daylight",
            "composition_camera": "eye-level shot",
            "style": "commercial photo",
            "quality": "high res",
        }
        # DALL-E: T2I without negative prompt parameter
        adapted = adapt_prompt_for_provider(raw_plan, "dall_e")
        self.assertEqual(adapted["provider_mode"], "text_to_image")
        self.assertIsNone(adapted["negative_prompt"])
        self.assertIn("office worker", adapted["positive_prompt"].lower())

    def test_build_image_plan_schema_coverage(self):
        plan = build_image_plan(
            source_ref="page-3",
            source_language="zh",
            canonical_scene_id="consumer.supermarket-shopping",
            scene_family="consumer",
            subject="consumer",
            environment="supermarket aisles",
            action_state="selecting goods",
            lighting="bright indoor lighting",
            composition_camera="wide angle shot",
            style="documentary realism",
            quality="high detail",
            provider_name="wikimedia-commons",
            asset_url="https://commons.wikimedia.org/wiki/File:Sample.jpg",
            license_name="Public domain",
            asset_sha256="a" * 64,
            slide_index=3,
        )
        expected_fields = [
            "source_ref", "source_language", "canonical_scene_id", "scene_family",
            "subject", "environment", "action_state", "lighting", "composition_camera",
            "style", "quality", "positive_prompt", "negative_prompt", "provider_mode",
            "provider_query", "asset_url", "license", "asset_sha256", "slide_index",
            "placement", "dedupe_decision", "review_status"
        ]
        for field in expected_fields:
            self.assertIn(field, plan, f"Field '{field}' missing from image plan")
        self.assertEqual(plan["slide_index"], 3)
        self.assertEqual(plan["provider_mode"], "photo_search")
        self.assertIsNone(plan["negative_prompt"])


if __name__ == "__main__":
    unittest.main()
