from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from workbench.make_flow import MakeOrchestrator


class WorkbenchSceneSnapshotTests(unittest.TestCase):
    def test_snapshot_exposes_scene_progress_and_gate_without_private_paths(self):
        with tempfile.TemporaryDirectory(prefix="wengao-scene-snapshot-") as temp:
            root = Path(temp)
            job_id = "job-scene-test"
            job_dir = root / "jobs" / job_id
            job_dir.mkdir(parents=True)
            (job_dir / "job.json").write_text(json.dumps({
                "job_id": job_id,
                "stages": [
                    {"stage": "semantic_scenes", "status": "RUNNING", "detail": {}},
                    {"stage": "semantic_scenes", "status": "DONE", "detail": {
                        "rules_version": "2.3.0", "detected_scene_ids": ["clinic_visit"],
                        "detected_source_languages": ["en", "es"],
                        "planned_photo_count": 1, "inserted": 1,
                        "image_pages": [2], "unresolved_scenes": [],
                    }},
                ],
            }), encoding="utf-8")
            (job_dir / "validation.json").write_text(json.dumps({
                "verdict": "NEEDS_REVIEW",
                "semantic_scene_gate": {
                    "status": "EXECUTED",
                    "run_directory": "/private/path/must-not-leak",
                    "summary": {"planned_scene_count": 1,
                                "embedded_scene_picture_count": 1,
                                "unique_embedded_photo_sha256_count": 1},
                    "findings": [],
                },
            }), encoding="utf-8")

            snapshot = MakeOrchestrator(root).snapshot(job_id)

        self.assertEqual(snapshot["runner"]["semantic_scene_stage"]["status"], "DONE")
        self.assertEqual(snapshot["runner"]["semantic_scene_stage"]["inserted"], 1)
        self.assertEqual(snapshot["runner"]["semantic_scene_stage"]["detected_source_languages"],
                         ["en", "es"])
        self.assertEqual(snapshot["validation"]["semantic_scene_gate"], {
            "status": "EXECUTED", "blocking": True, "planned_scene_count": 1,
            "embedded_scene_picture_count": 1,
            "unique_embedded_photo_count": 1, "rendered_scene_count": None,
            "findings": 0, "blocking_findings": 0, "unverified": 0,
        })
        self.assertNotIn("run_directory", json.dumps(snapshot))

    def test_snapshot_exposes_classified_model_failure_without_private_prompt(self):
        with tempfile.TemporaryDirectory(prefix="wengao-scene-snapshot-error-") as temp:
            root = Path(temp)
            job_id = "job-scene-error"
            job_dir = root / "jobs" / job_id
            job_dir.mkdir(parents=True)
            (job_dir / "job.json").write_text(json.dumps({
                "job_id": job_id,
                "verdict": "FAILED",
                "failure_detail": "do not leak source text",
                "stages": [{"stage": "semantic_scenes", "status": "FAILED",
                            "detail": {
                                "code": "MODEL.QUOTA_EXCEEDED",
                                "detail": "quota exhausted",
                                "model_error": {
                                    "code": "MODEL.QUOTA_EXCEEDED",
                                    "provider": "configured-local",
                                    "status_code": 429,
                                    "detail": "quota exhausted",
                                },
                                "source_text": "private slide text must not leak",
                            }}],
            }), encoding="utf-8")
            (job_dir / "validation.json").write_text("{}", encoding="utf-8")

            snapshot = MakeOrchestrator(root).snapshot(job_id)

        scene = snapshot["runner"]["semantic_scene_stage"]
        self.assertEqual(scene["status"], "FAILED")
        self.assertEqual(scene["failure_code"], "MODEL.QUOTA_EXCEEDED")
        self.assertEqual(scene["model_error"]["status_code"], 429)
        self.assertNotIn("source_text", json.dumps(snapshot))
        self.assertNotIn("private slide text", json.dumps(snapshot))


if __name__ == "__main__":
    unittest.main()
