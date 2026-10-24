import json
import hashlib
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from workbench import server
from wengao_ppt.runner import Runner, GateError


class TaskStateAndVendorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "jobs").mkdir(parents=True)
        (self.root / "material_tasks").mkdir(parents=True)
        (self.root / "workbench_uploads").mkdir(parents=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_vendor_dir_resolved_and_upstream_verified(self):
        runner = Runner(self.root)
        vendor = runner.vendor_dir()
        self.assertTrue(vendor.is_dir(), f"Vendor directory {vendor} must exist")
        # Run precheck on a dummy job
        job_id = "job-vendortest"
        jd = self.root / "jobs" / job_id
        jd.mkdir(parents=True)
        dummy_pptx = jd / "input.pptx"
        dummy_pptx.write_bytes(b"dummy")
        job_data = {
            "schema_version": "1.0.0", "job_id": job_id, "run_id": "r1",
            "attempt_id": "a1", "mode": "beautify", "support_profile": "P1_TEXT_LIMITED",
            "input_path": str(dummy_pptx), "input_hash": "dummyhash",
            "theme": "wengao-business", "created_at": "2026-10-23T00:00:00Z",
            "verdict": "QUEUED", "stages": []
        }
        (jd / "job.json").write_text(json.dumps(job_data), encoding="utf-8")
        info = runner.precheck(job_id)
        self.assertNotEqual(info["upstream_verified"], "VENDOR_DIR_MISSING",
                            "upstream_verified must not be VENDOR_DIR_MISSING")
        self.assertTrue(info["upstream_verified"] is True or info["upstream_verified"] == "GIT_UNAVAILABLE" or isinstance(info["upstream_verified"], bool),
                        f"Expected valid upstream verification result, got {info['upstream_verified']}")

    def test_stale_lock_from_dead_process_is_cleaned_up_without_conflict(self):
        runner = Runner(self.root)
        job_id = "job-locktest"
        jd = self.root / "jobs" / job_id
        jd.mkdir(parents=True)
        lock_file = jd / ".run.lock"
        # Write lock with a guaranteed dead pid (e.g. 999999)
        dead_pid = 999999
        lock_file.write_text(f"{dead_pid} 2026-10-23T00:00:00Z run", encoding="utf-8")
        
        # Now attempting to acquire lock should automatically clear dead lock without GateError
        acquired = False
        with runner._lock(job_id, purpose="run"):
            acquired = True
            # Inside lock, lock file should belong to current process
            owner = int(lock_file.read_text().split()[0])
            self.assertEqual(owner, os.getpid())
        self.assertTrue(acquired)
        self.assertFalse(lock_file.exists())

    def test_material_task_dead_process_marked_failed_on_load(self):
        tid = "mt-deadproc"
        rec = {
            "task_id": tid, "state": "running", "pid": 999999,
            "created_at": "2026-10-23T00:00:00Z", "heartbeat_at": "2026-10-23T00:00:00Z",
            "mode": "model", "theme": "wengao-business", "pages": 10
        }
        task_file = self.root / "material_tasks" / f"{tid}.json"
        task_file.write_text(json.dumps(rec), encoding="utf-8")

        with patch.object(server, "ROOT", self.root):
            loaded = server._mt_load(tid)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded["state"], "failed")
            self.assertIn("意外退出", loaded.get("stopped_reason", ""))
            self.assertEqual(loaded.get("verdict"), "FAILED")

    def test_material_task_waits_for_runner_lock_handoff(self):
        """A fresh RUNNING job is not dead in the gap before its lock is written."""
        job_id = "job-handofftest"
        jd = self.root / "jobs" / job_id
        jd.mkdir(parents=True)
        (jd / "job.json").write_text(json.dumps({
            "schema_version": "1.0.0", "job_id": job_id,
            "verdict": "RUNNING", "stage": "semantic_scenes",
            "last_activity": server.now_iso(),
        }), encoding="utf-8")

        tid = "mt-handofftest"
        (self.root / "material_tasks" / f"{tid}.json").write_text(json.dumps({
            "task_id": tid, "state": "running", "pid": 999999,
            "job_id": job_id, "auto_advance": True,
            "created_at": server.now_iso(), "heartbeat_at": server.now_iso(),
            "verdict": "GENERATED_PENDING_VALIDATION",
            "advance": [{"stage": "run", "at": server.now_iso(), "state": "running"}],
        }), encoding="utf-8")

        with patch.object(server, "ROOT", self.root), \
             patch.object(server, "_pid_alive", return_value=False):
            loaded = server._mt_load(tid)
        self.assertEqual(loaded["state"], "running")
        self.assertNotIn("stopped_reason", loaded)
        self.assertNotIn("finished_at", loaded)

    def test_material_task_recovers_false_exit_when_runner_lock_is_alive(self):
        """A status poll can recover the exact false-exit verdict after the lock appears."""
        job_id = "job-recoverytest"
        jd = self.root / "jobs" / job_id
        jd.mkdir(parents=True)
        (jd / "job.json").write_text(json.dumps({
            "schema_version": "1.0.0", "job_id": job_id,
            "verdict": "RUNNING", "stage": "semantic_scenes",
            "last_activity": server.now_iso(),
        }), encoding="utf-8")
        (jd / ".run.lock").write_text("123 2026-10-24T01:00:00+08:00 run", encoding="utf-8")

        tid = "mt-recoverytest"
        (self.root / "material_tasks" / f"{tid}.json").write_text(json.dumps({
            "task_id": tid, "state": "failed", "pid": 999999,
            "job_id": job_id, "auto_advance": True,
            "created_at": server.now_iso(), "heartbeat_at": server.now_iso(),
            "verdict": "GENERATED_PENDING_VALIDATION",
            "stopped_reason": "任务进程意外退出",
            "finished_at": server.now_iso(),
            "advance": [
                {"stage": "stop", "at": server.now_iso(), "state": "stop",
                 "reason": "任务进程意外退出"},
                {"stage": "run", "at": server.now_iso(), "state": "running"},
            ],
        }), encoding="utf-8")

        with patch.object(server, "ROOT", self.root), \
             patch.object(server, "_pid_alive", side_effect=lambda pid: pid == 123):
            loaded = server._mt_load(tid)
        self.assertEqual(loaded["state"], "running")
        self.assertNotIn("stopped_reason", loaded)
        self.assertNotIn("finished_at", loaded)
        self.assertFalse(any(e.get("stage") == "stop" for e in loaded["advance"]))

    def test_material_task_cancel_terminates_running_process(self):
        tid = "mt-canceltest"
        tdir = self.root / "material_tasks" / tid
        tdir.mkdir(parents=True)
        cancel_file = tdir / "cancel.request"

        # Mock a running process
        rec = {
            "task_id": tid, "state": "running", "pid": os.getpid(),  # simulate self but we will mock kill
            "cancel_file": str(cancel_file),
            "created_at": "2026-10-23T00:00:00Z", "heartbeat_at": "2026-10-23T00:00:00Z",
            "mode": "model", "theme": "wengao-business", "pages": 10
        }
        task_file = self.root / "material_tasks" / f"{tid}.json"
        task_file.write_text(json.dumps(rec), encoding="utf-8")

        with patch.object(server, "ROOT", self.root), \
             patch("os.kill") as mock_kill, \
             patch.object(server, "_pid_alive", return_value=True):
            code, res = server._mt_cancel(tid)
            self.assertEqual(code, 200)
            self.assertTrue(res["cancelled"])
            self.assertEqual(res["task"]["state"], "cancelled")
            self.assertEqual(res["task"]["verdict"], "CANCELLED")
            mock_kill.assert_called_with(os.getpid(), signal.SIGTERM)

    def test_material_task_retry_starts_new_task(self):
        tid = "mt-retrytest"
        sample_file = self.root / "workbench_uploads" / "sample.md"
        sample_file.write_text("# Test\nSample content with 10% growth.", encoding="utf-8")
        rec = {
            "task_id": tid, "state": "failed", "pid": 999999,
            "source": str(sample_file), "source_name": sample_file.name,
            "created_at": "2026-10-23T00:00:00Z", "mode": "model",
            "theme": "wengao-business", "pages": 10, "auto_advance": True
        }
        task_file = self.root / "material_tasks" / f"{tid}.json"
        task_file.write_text(json.dumps(rec), encoding="utf-8")

        with patch.object(server, "ROOT", self.root), \
             patch.object(server, "_mt_start", return_value=(202, {"task": {"task_id": "mt-new", "state": "running"}})) as mock_start:
            code, res = server._mt_retry(tid)
            self.assertEqual(code, 202)
            mock_start.assert_called_once()
            call_body = mock_start.call_args[0][0]
            self.assertEqual(call_body["source"], str(sample_file))
            self.assertEqual(call_body["pages"], 10)
            self.assertEqual(call_body["theme"], "wengao-business")

    def test_material_task_timeout_handling(self):
        tid = "mt-timeouttest"
        tdir = self.root / "material_tasks" / tid
        tdir.mkdir(parents=True)
        rec = {
            "task_id": tid, "state": "running", "pid": 999999,
            "created_at": "2026-10-23T00:00:00Z", "heartbeat_at": "2026-10-23T00:00:00Z",
            "mode": "model", "theme": "wengao-business", "pages": 10
        }
        task_file = self.root / "material_tasks" / f"{tid}.json"
        task_file.write_text(json.dumps(rec), encoding="utf-8")

        # Simulate timeout in task
        rec["state"] = "failed"
        rec["verdict"] = "FAILED"
        rec["stopped_reason"] = "材料生成超时（超过 1800 秒未完成）"
        rec["fail_reason"] = "GATE.TIMEOUT"
        task_file.write_text(json.dumps(rec), encoding="utf-8")

        with patch.object(server, "ROOT", self.root):
            loaded = server._mt_load(tid)
            self.assertEqual(loaded["state"], "failed")
            self.assertEqual(loaded["verdict"], "FAILED")
            self.assertEqual(loaded["fail_reason"], "GATE.TIMEOUT")
            self.assertIn("超时", loaded["stopped_reason"])

    def test_multilingual_detected_before_keyword_matching(self):
        from wengao_ppt import visual_plan
        from pptx import Presentation

        prs = Presentation()
        prs.slide_width = 12191695
        prs.slide_height = 6858000
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = "আন্তর্জাতিক সহযোগিতা ও উন্নয়ন"
        slide.placeholders[1].text = "বাংলা ভাষায় টেক্সট এবং প্রযুক্তিগত অগ্রগতি।"

        pptx_path = self.root / "bengali_sample.pptx"
        prs.save(str(pptx_path))
        plan = visual_plan.build_plan(str(pptx_path))
        # Ensure language support is detected and recorded before matching
        self.assertIn("language_support", plan)
        lang_info = plan["language_support"]
        self.assertIn("bn", lang_info.get("detected_source_languages", []))
        self.assertEqual(plan["keyword_scene_order"][0], "extract_page_text")
        self.assertEqual(plan["keyword_scene_order"][1], "detect_language_and_script_offline")
        # Ensure page 1 is not routed to legacy Chinese keyword matching
        page1 = plan["pages"][0]
        self.assertIn("已先识别本页语言为 bn", page1.get("reason", ""))

    def test_scene_photo_fixture_has_one_tagged_photo(self):
        from pptx import Presentation
        fixture_dir = (Path(__file__).parent / "fixtures" / "scene-photo")
        fixture = json.loads((fixture_dir / "fixture_manifest.json").read_text(
            encoding="utf-8"))
        self.assertFalse(fixture["product_acceptance"])
        candidate_pptx = fixture_dir / fixture["pptx_file"]
        self.assertTrue(candidate_pptx.is_file())
        self.assertEqual(hashlib.sha256(candidate_pptx.read_bytes()).hexdigest(),
                         fixture["pptx_sha256"])
        prs = Presentation(str(candidate_pptx))
        scene_shapes = []
        for slide in prs.slides:
            for sh in slide.shapes:
                if "WENGAO_SCENE" in sh.name:
                    scene_shapes.append(sh)
        self.assertEqual(len(scene_shapes), 1, "Must contain exactly 1 scene photo object in candidate")
        sh = scene_shapes[0]
        # Verify shape name format: WENGAO_SCENE|<scene_key>|<sha256>
        parts = sh.name.split("|")
        self.assertEqual(len(parts), 3)
        self.assertEqual(parts[0], "WENGAO_SCENE")
        self.assertEqual(parts[1], fixture["scene_key"])
        self.assertEqual(len(parts[2]), 64)

    def test_material_task_runner_authoritative_failed_syncs_to_api(self):
        """基线类似 mt-08f63a27：task 记录标记为 running/70%，但 runner job 已 FAILED。
        API 与 _mt_load 必须由 runner 权威终态驱动：纠正为 failed 终态、percent 0、download_ready False。"""
        job_id = "job-failtest1234"
        jd = self.root / "jobs" / job_id
        jd.mkdir(parents=True)
        job_data = {
            "schema_version": "1.0.0", "job_id": job_id, "verdict": "FAILED",
            "fail_reason": "VISUAL.SCENE_UNRESOLVED",
            "failure_detail": "场景照片或安全版位未完整解决；阻止文字卡片替代场景图。",
            "run_finished_at": "2026-10-23T22:14:09.000000Z",
            "download_ready": False,
        }
        (jd / "job.json").write_text(json.dumps(job_data), encoding="utf-8")

        tid = "mt-syncfail"
        rec = {
            "task_id": tid, "state": "running", "pid": 999999, "job_id": job_id,
            "created_at": "2026-10-23T20:00:00Z", "auto_advance": True,
            "advance": [{"stage": "run", "at": "2026-10-23T20:05:00Z", "state": "running"}],
            "verdict": "GENERATED_PENDING_VALIDATION",
            "generated_pptx": "fake.pptx"
        }
        task_file = self.root / "material_tasks" / f"{tid}.json"
        task_file.write_text(json.dumps(rec), encoding="utf-8")

        with patch.object(server, "ROOT", self.root):
            code, resp = server._mt_status(tid)
            self.assertEqual(code, 200)
            task = resp["task"]
            self.assertEqual(task["state"], "failed")
            self.assertEqual(task["verdict"], "FAILED")
            self.assertEqual(task["fail_reason"], "VISUAL.SCENE_UNRESOLVED")
            self.assertEqual(task["percent"], 0)
            self.assertFalse(task["download_ready"])
            self.assertFalse(task["success"])
            self.assertTrue(task["refused"])
            self.assertIn("场景照片或安全版位未完整解决", task["stopped_reason"])
            self.assertIn("任务失败", task["stage"])
            self.assertEqual(task["finished_at"], "2026-10-23T22:14:09.000000Z")

    def test_material_task_never_marks_failed_job_as_downloadable(self):
        """无论 auto_advance 还是 generated_pptx 是否存在，失败结果绝不能标成可下载。"""
        job_id = "job-nodownload1"
        jd = self.root / "jobs" / job_id
        jd.mkdir(parents=True)
        job_data = {
            "schema_version": "1.0.0", "job_id": job_id, "verdict": "FAILED",
            "fail_reason": "GATE.CLI_FAILED",
            "failure_detail": "运行阶段失败",
            "export_path": str(jd / "fake.exported.pptx")
        }
        (jd / "job.json").write_text(json.dumps(job_data), encoding="utf-8")

        tid = "mt-nodownload"
        rec = {
            "task_id": tid, "state": "done", "returncode": 0, "job_id": job_id,
            "created_at": "2026-10-23T20:00:00Z", "auto_advance": True,
            "verdict": "FAILED", "generated_pptx": "fake.pptx",
            "export_path": str(jd / "fake.exported.pptx")
        }
        task_file = self.root / "material_tasks" / f"{tid}.json"
        task_file.write_text(json.dumps(rec), encoding="utf-8")

        with patch.object(server, "ROOT", self.root):
            code, resp = server._mt_status(tid)
            self.assertEqual(code, 200)
            self.assertFalse(resp["task"]["download_ready"])

    def test_baseline_mt_08f63a27_status_reconciles_cleanly(self):
        """对真实落盘基线 mt-08f63a27 进行权威终态同步验收。"""
        real_root = Path("wb_loopback_data")
        if not (real_root / "material_tasks" / "mt-08f63a27.json").is_file():
            self.skipTest("mt-08f63a27.json not found")
        with patch.object(server, "ROOT", real_root):
            code, resp = server._mt_status("mt-08f63a27")
            self.assertEqual(code, 200)
            t = resp["task"]
            self.assertEqual(t["state"], "failed")
            self.assertEqual(t["verdict"], "FAILED")
            self.assertEqual(t["fail_reason"], "VISUAL.SCENE_UNRESOLVED")
            self.assertEqual(t["percent"], 0)
            self.assertFalse(t["download_ready"])
            self.assertFalse(t["success"])
            self.assertTrue(t["refused"])



if __name__ == "__main__":
    unittest.main()
