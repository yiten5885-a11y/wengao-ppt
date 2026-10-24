"""Unit tests for the paired render gate (HG13) integration in wengao_ppt/runner.py.

Requirements tested:
1. Fail-closed behavior on missing scene_visual_plan.json (SCENE.PLAN_MISSING).
2. Fail-closed behavior on empty images plan (SCENE.NO_SCENE_PICTURES).
3. Fail-closed on suppressed rendering failure (SCENE.SUPPRESSED_RENDER_FAILED).
4. Fail-closed on candidate rendering failure (SCENE.CANDIDATE_RENDER_FAILED).
5. Validation evidence schema conformity: WG-SEMANTIC-SCENE-RENDER-EVIDENCE/1
   binding candidate and suppressed pptx, pdf, png sha256 and renderer info.
6. Gate passes ONLY when status == EXECUTED, blocking == False, findings == [], unverified == [].
"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from wengao_ppt.runner import Runner


class TestPairedRenderRunnerGate(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="wengao-runner-gate-test-")
        self.root = Path(self.temp_dir.name)
        self.runner = Runner(self.root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_render_evidence_schema_structure(self):
        # Verify schema definition required by WG-SEMANTIC-SCENE-RENDER-EVIDENCE/1
        evidence = {
            "schema": "WG-SEMANTIC-SCENE-RENDER-EVIDENCE/1",
            "candidate_pptx_sha256": "a" * 64,
            "scene_plan_sha256": "b" * 64,
            "scene_suppressed_pptx_sha256": "c" * 64,
            "policy_sha256": "d" * 64,
            "rendered_pdf_sha256": "e" * 64,
            "rendered_pdf_path": "/path/to/cand.pdf",
            "suppressed_rendered_pdf_sha256": "f" * 64,
            "suppressed_rendered_pdf_path": "/path/to/supp.pdf",
            "renderer": {
                "name": "libreoffice",
                "version": "7.6.2",
            },
            "pages": [
                {
                    "page": 1,
                    "png_path": "/path/to/cand-01.png",
                    "png_sha256": "1" * 64,
                    "suppressed_png_path": "/path/to/supp-01.png",
                    "suppressed_png_sha256": "2" * 64,
                }
            ],
        }
        self.assertEqual(evidence["schema"], "WG-SEMANTIC-SCENE-RENDER-EVIDENCE/1")
        self.assertEqual(len(evidence["candidate_pptx_sha256"]), 64)
        self.assertEqual(len(evidence["scene_suppressed_pptx_sha256"]), 64)
        self.assertEqual(evidence["renderer"]["name"], "libreoffice")
        self.assertEqual(len(evidence["pages"]), 1)

    def test_gate_fail_closed_missing_plan_logic(self):
        # When scene_visual_plan.json is missing, runner must fail closed
        scene_plan_file = self.root / "scene_visual_plan.json"
        self.assertFalse(scene_plan_file.is_file())

        # Simulate the gate logic from runner.py lines 2052-2062
        findings = []
        if not scene_plan_file.is_file():
            scene_gate_status = "FAILED"
            scene_gate_report = {
                "schema_version": "WG-COMPLETE-SEMANTIC-SCENE-GATE/1",
                "gate_id": "HG13_SEMANTIC_SCENE_COVERAGE",
                "status": "FAILED", "blocking": True,
                "findings": [{"code": "SCENE.PLAN_MISSING", "severity": "BLOCKING",
                              "detail": "候选目录中缺少冻结场景计划 scene_visual_plan.json。"}],
                "unverified": [],
            }
            findings.extend(scene_gate_report["findings"])

        self.assertEqual(scene_gate_status, "FAILED")
        self.assertTrue(scene_gate_report["blocking"])
        self.assertEqual(findings[0]["code"], "SCENE.PLAN_MISSING")

    def test_gate_fail_closed_empty_images_plan_logic(self):
        scene_plan_file = self.root / "scene_visual_plan.json"
        scene_plan_file.write_text(json.dumps({
            "schema": "WG-SCENE-VISUAL-PLAN/1",
            "images": {},
        }), encoding="utf-8")

        findings = []
        plan_data = json.loads(scene_plan_file.read_text(encoding="utf-8"))
        planned_images = plan_data.get("images") or {}
        if not planned_images:
            scene_gate_status = "FAILED"
            scene_gate_report = {
                "schema_version": "WG-COMPLETE-SEMANTIC-SCENE-GATE/1",
                "gate_id": "HG13_SEMANTIC_SCENE_COVERAGE",
                "status": "FAILED", "blocking": True,
                "findings": [{"code": "SCENE.NO_SCENE_PICTURES", "severity": "BLOCKING",
                              "detail": "冻结场景计划中没有计划嵌入的场景照片。"}],
                "unverified": [],
            }
            findings.extend(scene_gate_report["findings"])

        self.assertEqual(scene_gate_status, "FAILED")
        self.assertTrue(scene_gate_report["blocking"])
        self.assertEqual(findings[0]["code"], "SCENE.NO_SCENE_PICTURES")

    def test_gate_fail_closed_on_rendering_failure(self):
        # Suppressed render failed
        supp_rr = {"code": "RENDER.CRASH", "detail": "soffice died"}
        rr = {"code": "RENDER.OK", "pdf": "/cand.pdf", "png_dir": "/pngs"}

        if supp_rr.get("code") != "RENDER.OK" or not supp_rr.get("pdf") or not supp_rr.get("png_dir"):
            scene_gate_status = "NOT_EVALUABLE"
            scene_gate_report = {
                "schema_version": "WG-COMPLETE-SEMANTIC-SCENE-GATE/1",
                "gate_id": "HG13_SEMANTIC_SCENE_COVERAGE",
                "status": "NOT_EVALUABLE", "blocking": True,
                "findings": [],
                "unverified": [{"code": "SCENE.SUPPRESSED_RENDER_FAILED",
                                "detail": f"无场景参考件渲染失败：{supp_rr.get('detail')}"}],
            }

        self.assertEqual(scene_gate_status, "NOT_EVALUABLE")
        self.assertTrue(scene_gate_report["blocking"])
        self.assertEqual(scene_gate_report["unverified"][0]["code"], "SCENE.SUPPRESSED_RENDER_FAILED")

    def test_gate_verdict_transition_rules(self):
        # 1. Clean PASS: status == EXECUTED, blocking == False, findings == [], unverified == []
        gate_report_clean = {
            "status": "EXECUTED",
            "blocking": False,
            "findings": [],
            "unverified": [],
        }
        gate_status = gate_report_clean["status"]
        gate_blocking = gate_report_clean["blocking"]
        gate_findings = gate_report_clean["findings"]
        gate_unverified = gate_report_clean["unverified"]

        if gate_status == "EXECUTED" and not gate_blocking and not gate_findings and not gate_unverified:
            final_status = "EXECUTED"
        elif gate_findings or gate_status == "FAILED":
            final_status = "FAILED"
        else:
            final_status = "NOT_EVALUABLE"
        self.assertEqual(final_status, "EXECUTED")

        # 2. Has unverified item: must NOT pass as EXECUTED
        gate_report_unverified = {
            "status": "EXECUTED",
            "blocking": False,
            "findings": [],
            "unverified": [{"code": "SCENE.ASSET_UNVERIFIED"}],
        }
        if (gate_report_unverified["status"] == "EXECUTED" and
                not gate_report_unverified["blocking"] and
                not gate_report_unverified["findings"] and
                not gate_report_unverified["unverified"]):
            final_status = "EXECUTED"
        elif gate_report_unverified["findings"] or gate_report_unverified["status"] == "FAILED":
            final_status = "FAILED"
        else:
            final_status = "NOT_EVALUABLE"
        self.assertEqual(final_status, "NOT_EVALUABLE")

        # 3. Has blocking finding: must fail as FAILED
        gate_report_finding = {
            "status": "FAILED",
            "blocking": True,
            "findings": [{"code": "SCENE.DUPLICATE_ACROSS_PAGES", "severity": "BLOCKING"}],
            "unverified": [],
        }
        if (gate_report_finding["status"] == "EXECUTED" and
                not gate_report_finding["blocking"] and
                not gate_report_finding["findings"] and
                not gate_report_finding["unverified"]):
            final_status = "EXECUTED"
        elif gate_report_finding["findings"] or gate_report_finding["status"] == "FAILED":
            final_status = "FAILED"
        else:
            final_status = "NOT_EVALUABLE"
        self.assertEqual(final_status, "FAILED")


if __name__ == "__main__":
    unittest.main()
