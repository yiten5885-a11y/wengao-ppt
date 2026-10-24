from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from wengao_ppt.runner import (
    ALWAYS_REQUIRED_CHECKS,
    P2_CHART_SUPPORT_PROFILE,
    P2_SUPPORT_PROFILE,
    SUPPORT_PROFILE,
    Runner,
    expected_required_checks,
)


class SemanticSceneGlobalGateTests(unittest.TestCase):
    def test_hg13_is_required_for_every_supported_pptx_profile(self):
        self.assertIn("visual.semantic_scene_coverage", ALWAYS_REQUIRED_CHECKS)
        for profile in (SUPPORT_PROFILE, P2_SUPPORT_PROFILE, P2_CHART_SUPPORT_PROFILE):
            with self.subTest(profile=profile):
                required = expected_required_checks(profile)["required"]
                self.assertIn("visual.semantic_scene_coverage", required)

    def test_scene_planning_failure_is_terminal_and_clears_worker_pid(self):
        with tempfile.TemporaryDirectory(prefix="wengao-scene-terminal-") as temp:
            runner = Runner(temp)
            job_id = "job-scene-failure"
            job_dir = runner._job_dir(job_id)
            job_dir.mkdir(parents=True)
            (job_dir / "job.json").write_text(json.dumps({
                "job_id": job_id,
                "verdict": "RUNNING",
                "stage": "semantic_scenes",
                "stages": [{"stage": "semantic_scenes", "status": "FAILED"}],
            }), encoding="utf-8")
            runner._pid_file(job_id).write_text("12345", encoding="utf-8")

            runner._mark_run_stage_failure(
                job_id, "MODEL.HTTP_ERROR", "local model unavailable",
                model_error={"code": "MODEL.HTTP_ERROR", "provider": "ollama-local"},
            )

            job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
            self.assertEqual(job["verdict"], "FAILED")
            self.assertEqual(job["fail_reason"], "MODEL.HTTP_ERROR")
            self.assertEqual(job["model_error"]["provider"], "ollama-local")
            self.assertFalse(runner._pid_file(job_id).exists())


if __name__ == "__main__":
    unittest.main()
