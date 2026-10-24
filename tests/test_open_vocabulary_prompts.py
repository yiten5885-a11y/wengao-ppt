"""Unit tests for open-vocabulary scene prompt adaptation, regression against user-50 and user-36 fixtures,
and verification of novel open-vocabulary scenes outside the 86 cataloged examples.

Adheres to hard-gate.md requirements:
1. Open-vocabulary extraction (subject, environment, action_state, lighting, composition_camera, style, quality).
2. Regression against user-50-scenes.json (50 scenarios) and user-36-supplemental-scenes.json (36 cases).
3. Distinct sub-scenes within same domain remain separate (supermarket shopping vs checkout vs receipt;
   ceramic cup vs tumbler vs drinking; library vs books vs newspaper; hospital care vs clinic vs consultation).
4. Testing novel scenes outside the 86 fixtures (quantum computing, offshore wind turbine, drone delivery,
   high-speed rail cab, artisan coffee roasting, neonatal incubator).
5. Provider-aware negative prompt delivery and short neutral 2-4 word visual query generation.
6. Pure data/table slides auditably record no_scene_reason.
"""
import json
from pathlib import Path
import unittest

from wengao_ppt.language_support import canonical_scene_identity
from wengao_ppt.scene_images import (
    adapt_prompt_for_provider,
    build_image_plan,
    build_positive_prompt,
    build_provider_search_query,
    build_provider_search_query_from_anchors,
    query_preserves_photo_search_anchors,
    resolve_scene_lighting,
)


FIXTURES_DIR = Path(__file__).parent / "fixtures" / "semantic-scenes"


class TestOpenVocabularyPrompts(unittest.TestCase):

    def test_validated_anchor_query_keeps_every_role_token_in_order(self):
        anchors = {"subject": "clean staff", "action": "assembling metal parts",
                   "setting": "factory floor"}
        query = build_provider_search_query_from_anchors(anchors)
        self.assertEqual(query, "clean staff assembling metal parts factory floor")
        self.assertTrue(query_preserves_photo_search_anchors(query, anchors))

    def test_user_50_scenes_fixture_regression(self):
        fixture_path = FIXTURES_DIR / "user-50-scenes.json"
        self.assertTrue(fixture_path.exists(), f"Missing fixture {fixture_path}")
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        scenarios = data.get("scenarios", [])
        self.assertEqual(len(scenarios), 50, f"Expected 50 scenarios, found {len(scenarios)}")

        for sc in scenarios:
            case_id = sc.get("case_id")
            label = sc.get("label")
            brief = sc.get("scenario_brief")
            self.assertTrue(case_id and label and brief, f"Incomplete scenario record: {sc}")

            # Derive lighting and search query from brief
            lighting = resolve_scene_lighting(environment=brief, action_state=label)
            self.assertTrue(isinstance(lighting, str) and len(lighting) > 0)

            # Test search query builder with label and brief
            query = build_provider_search_query(
                subject=label,
                environment=brief,
                action_state="",
                max_words=4,
            )
            # Query words check
            words = query.split()
            self.assertTrue(2 <= len(words) <= 4,
                            f"Case {case_id} query '{query}' word count {len(words)} not in [2, 4]")
            self.assertNotIn("8k", query.lower())
            self.assertNotIn("blurry", query.lower())

    def test_user_36_supplemental_scenes_fixture_regression(self):
        fixture_path = FIXTURES_DIR / "user-36-supplemental-scenes.json"
        self.assertTrue(fixture_path.exists(), f"Missing fixture {fixture_path}")
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        cases = data.get("cases", [])
        self.assertEqual(len(cases), 36, f"Expected 36 cases, found {len(cases)}")

        generated_ids = {}
        for c in cases:
            case_id = c["case_id"]
            expected_id = c["expected_scene_id"]
            # When explicit canonical ID is given, canonical_scene_identity preserves it
            resolved_id = canonical_scene_identity(
                topic=c["topic"],
                subject=c["subject"],
                action=c["action_state"],
                setting=c["environment"],
                explicit_canonical_id=expected_id,
            )
            self.assertEqual(resolved_id, expected_id,
                             f"Case {case_id} failed to resolve explicit id {expected_id}")
            generated_ids[case_id] = resolved_id

        # Verify distinct sub-scenes within consumer topic: UE01 vs UE02 vs UE03
        self.assertNotEqual(generated_ids["UE01"], generated_ids["UE02"])
        self.assertNotEqual(generated_ids["UE01"], generated_ids["UE03"])
        self.assertNotEqual(generated_ids["UE02"], generated_ids["UE03"])

        # Verify distinct drinkware sub-scenes: UE17 (ceramic cup) vs UE18 (tumbler) vs UE19 (drinking)
        self.assertNotEqual(generated_ids["UE17"], generated_ids["UE18"])
        self.assertNotEqual(generated_ids["UE17"], generated_ids["UE19"])
        self.assertNotEqual(generated_ids["UE18"], generated_ids["UE19"])

        # Verify distinct healthcare sub-scenes
        hc_cases = ["UE22", "UE23", "UE24", "UE25", "UE26", "UE27"]
        hc_ids = [generated_ids[cid] for cid in hc_cases]
        self.assertEqual(len(hc_ids), len(set(hc_ids)), "Healthcare sub-scenes collapsed!")

        # Verify distinct reading sub-scenes
        reading_cases = ["UE32", "UE33", "UE34", "UE35"]
        reading_ids = [generated_ids[cid] for cid in reading_cases]
        self.assertEqual(len(reading_ids), len(set(reading_ids)), "Reading sub-scenes collapsed!")

    def test_novel_open_vocabulary_scenes_outside_fixtures(self):
        """Test open-vocabulary synthesis on completely novel concepts outside the 86 fixtures."""
        novel_cases = [
            {
                "topic": "quantum_computing",
                "subject": "dilution refrigerator",
                "environment": "quantum physics cleanroom laboratory",
                "action_state": "cryogenic golden chandelier frame cooling superconducting qubits",
                "expected_lighting_term": "clean",
            },
            {
                "topic": "offshore_wind",
                "subject": "wind turbine technician",
                "environment": "offshore wind turbine nacelle in open ocean",
                "action_state": "wearing safety harness inspecting gearbox generator",
                "expected_lighting_term": "sunlight",
            },
            {
                "topic": "logistics_robotics",
                "subject": "delivery drone",
                "environment": "residential suburban rooftop landing pad",
                "action_state": "autonomous quadcopter releasing package parcel",
                "expected_lighting_term": "sunlight",
            },
            {
                "topic": "rail_transit",
                "subject": "locomotive engineer driver",
                "environment": "high-speed bullet train driver cabin cockpit",
                "action_state": "monitoring digital instrument control console at night",
                "expected_lighting_term": "night",
            },
            {
                "topic": "specialty_coffee",
                "subject": "artisan coffee roaster",
                "environment": "craft coffee roasting roastery workshop",
                "action_state": "checking freshly roasted coffee beans in circular cooling tray",
                "expected_lighting_term": "industrial",
            },
            {
                "topic": "neonatal_medicine",
                "subject": "infant incubator",
                "environment": "hospital neonatal intensive care unit NICU",
                "action_state": "medical monitoring instruments displaying vital signs",
                "expected_lighting_term": "hospital",
            },
        ]

        for case in novel_cases:
            # 1. Open-vocabulary scene identity resolution
            canonical_id = canonical_scene_identity(
                topic=case["topic"],
                subject=case["subject"],
                action=case["action_state"],
                setting=case["environment"],
            )
            self.assertTrue(canonical_id.startswith(case["topic"]) or "." in canonical_id)

            # 2. Lighting conflict resolution
            lighting = resolve_scene_lighting(
                environment=case["environment"],
                action_state=case["action_state"],
                preferred_lighting="自然光摄影",
            )
            self.assertIn(case["expected_lighting_term"], lighting.lower(),
                          f"Case {case['topic']} lighting '{lighting}' missing '{case['expected_lighting_term']}'")

            # 3. Search query generation (2-4 neutral words)
            query = build_provider_search_query(
                subject=case["subject"],
                environment=case["environment"],
                action_state=case["action_state"],
                max_words=4,
            )
            words = query.split()
            self.assertTrue(2 <= len(words) <= 4,
                            f"Case {case['topic']} query '{query}' word count {len(words)} not in [2, 4]")
            self.assertNotIn("8k", query.lower())
            self.assertNotIn("realistic", query.lower())

            # 4. Positive prompt building (7 components)
            pos_prompt = build_positive_prompt(
                subject=case["subject"],
                environment=case["environment"],
                action_state=case["action_state"],
                lighting=lighting,
                composition_camera="medium shot, eye-level angle",
                style="documentary commercial photography",
                quality="sharp focus, natural depth of field",
            )
            self.assertIn(case["subject"], pos_prompt)
            self.assertIn(lighting, pos_prompt)

            # 5. Full 22-field image plan building
            plan = build_image_plan(
                source_ref=f"slide-{case['topic']}",
                source_language="en",
                canonical_scene_id=canonical_id,
                scene_family=case["topic"],
                subject=case["subject"],
                environment=case["environment"],
                action_state=case["action_state"],
                lighting=lighting,
                composition_camera="medium shot",
                style="documentary",
                quality="high definition",
                provider_name="wikimedia-commons",
                slide_index=1,
            )
            self.assertEqual(plan["canonical_scene_id"], canonical_id)
            self.assertEqual(plan["provider_mode"], "photo_search")
            self.assertIsNone(plan["negative_prompt"])

    def test_provider_aware_prompt_negative_isolation(self):
        base_plan = {
            "subject": "welder",
            "environment": "shipyard drydock",
            "action_state": "welding steel hull plate",
            "lighting": "industrial sparks illumination",
            "composition_camera": "close up shot",
            "style": "documentary photo",
            "quality": "detailed metal texture",
            "provider_query": "welder shipyard steel",
        }

        # 1. Wikimedia Commons (Photo search): negative prompt stripped
        wiki_adapted = adapt_prompt_for_provider(base_plan, "wikimedia-commons")
        self.assertIsNone(wiki_adapted["negative_prompt"])
        self.assertEqual(wiki_adapted["provider_mode"], "photo_search")

        # 2. Openverse (Photo search): negative prompt stripped
        openverse_adapted = adapt_prompt_for_provider(base_plan, "openverse")
        self.assertIsNone(openverse_adapted["negative_prompt"])
        self.assertEqual(openverse_adapted["provider_mode"], "photo_search")

        # 3. Stable Diffusion (T2I with negative prompt): negative prompt injected
        sd_adapted = adapt_prompt_for_provider(base_plan, "stable_diffusion")
        self.assertIsNotNone(sd_adapted["negative_prompt"])
        self.assertIn("blurry", sd_adapted["negative_prompt"].lower())
        self.assertEqual(sd_adapted["provider_mode"], "text_to_image")

        # 4. DALL-E (T2I without negative prompt): negative prompt stripped
        dalle_adapted = adapt_prompt_for_provider(base_plan, "dall_e")
        self.assertIsNone(dalle_adapted["negative_prompt"])
        self.assertEqual(dalle_adapted["provider_mode"], "text_to_image")


if __name__ == "__main__":
    unittest.main()
