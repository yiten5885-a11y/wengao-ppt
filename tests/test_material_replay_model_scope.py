import json
import unittest
from pathlib import Path

from workbench import server


ROOT = Path(__file__).resolve().parents[1]


class MaterialReplayModelScopeTests(unittest.TestCase):
    def test_replay_mode_discloses_downstream_scene_model_and_network_stages(self):
        usage = server._material_model_usage("replay")
        self.assertEqual(usage["content_plan"], "frozen_replay_no_model_call")
        self.assertEqual(usage["semantic_scene_planning"], "local_model_may_be_called")
        self.assertEqual(usage["photo_search"], "provider_network_may_be_used")

    def test_workbench_copy_never_claims_replay_skips_all_model_calls(self):
        ui = (ROOT / "workbench" / "index.html").read_text(encoding="utf-8")
        meta = (ROOT / "workbench" / "server.py").read_text(encoding="utf-8")
        packs = json.loads((ROOT / "samples" / "material_replay" / "index.json").read_text(
            encoding="utf-8"))
        self.assertIn("跳过内容规划模型", ui)
        self.assertIn("语义配图仍可能调用本地模型", ui)
        self.assertNotIn("回放（不调用模型）", ui)
        self.assertIn("下游语义配图仍可能调用本地模型", meta)
        self.assertIn("跳过内容规划模型", packs["note"])


if __name__ == "__main__":
    unittest.main()
