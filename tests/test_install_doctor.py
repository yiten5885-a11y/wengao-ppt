"""`wengao doctor`：安装完整性 vs 运行前置必须分开报。

背景（2026-11-04 干净安装实测）：干净安装根上首任务失败的真实原因是**本机没有视觉
端点**，但对外只看到一句 `MODEL.VISION_RUNTIME_UNAVAILABLE`；用户无法从安装状态判断
"是装坏了，还是环境没就绪"。doctor 把两类分开：blocking（安装完整性）与
prerequisite（一次 run 的外部前置），且 doctor 本身必须只读。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wengao_ppt import install, scene_model


class DoctorOnEmptyRootTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="doctor-empty-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def test_reports_not_installed_as_blocking(self):
        rc = install.main(["--root", str(self.root), "doctor", "--json"])
        self.assertEqual(rc, 2)

    def test_does_not_create_data_dir(self):
        """doctor 是只读诊断：不能在目标根下建 data/（那会留下空壳安装）。"""
        install.main(["--root", str(self.root), "doctor", "--json"])
        self.assertFalse((self.root / "data").exists(),
                         "doctor 在未安装的根下创建了 data/")


class DoctorPayloadShapeTests(unittest.TestCase):
    def test_checks_are_typed_by_severity(self):
        root = Path(tempfile.mkdtemp(prefix="doctor-shape-"))
        try:
            import contextlib
            import io
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                install.do_doctor(root, as_json=True)
            out = json.loads(buf.getvalue())
            self.assertIn("install_ok", out)
            self.assertIn("run_ready", out)
            severities = {c["severity"] for c in out["checks"]}
            self.assertEqual(severities, {"blocking", "prerequisite"})
            # 未安装时 blocking 里必须有 version_resolved，且不得把"未安装"算成 run_ready
            self.assertIn("version_resolved", out["blocking_missing"])
            self.assertFalse(out["run_ready"])
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class FontCheckTests(unittest.TestCase):
    def test_font_check_uses_the_install_data_root(self):
        """字体检查必须按**本安装根的数据根**构造，否则 doctor 会误报缓存位置。

        真实 CLI 以 `--project-root <root>/data` 运行；doctor 若调用
        `fontenv.build_env()` 不带 root，缓存会落到 ~/.cache，于是"装好了却报前置缺失"。
        """
        from wengao_ppt import fontenv
        root = Path(tempfile.mkdtemp(prefix="doctor-font-"))
        seen = {}
        real = fontenv.build_env

        def spy(rt=None):
            seen["root"] = rt
            return real(rt)

        try:
            import contextlib
            import io
            # 字体配置来自程序目录；未安装时无从检查（也不该写盘），所以这里必须
            # 伪装成"已安装"，才有东西可验。
            fake_prog = root / "versions" / "v1"
            with mock.patch.object(install, "current_prog", return_value=fake_prog), \
                    mock.patch.object(fontenv, "build_env", side_effect=spy):
                with contextlib.redirect_stdout(io.StringIO()):
                    install.do_doctor(root, as_json=True)
            self.assertEqual(Path(str(seen["root"])), root / "data")
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    def test_font_check_skipped_when_not_installed(self):
        """未安装时不得调用 fontenv：build_env() 会在数据根建缓存 = 偷偷写盘。"""
        from wengao_ppt import fontenv
        root = Path(tempfile.mkdtemp(prefix="doctor-font-skip-"))
        try:
            import contextlib
            import io
            with mock.patch.object(fontenv, "build_env",
                                   side_effect=AssertionError("未安装时不应探测字体")):
                with contextlib.redirect_stdout(io.StringIO()):
                    install.do_doctor(root, as_json=True)
            self.assertFalse((root / "data").exists())
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)


class VisionProbeTests(unittest.TestCase):
    def test_unavailable_probe_carries_candidates_and_actions(self):
        with mock.patch.object(scene_model, "_probe_endpoint_health",
                               return_value=(False, {})):
            rec = scene_model.probe_vision_endpoint()
        self.assertFalse(rec["available"])
        self.assertEqual(rec["candidates"], ["127.0.0.1:8080", "127.0.0.1:11434"])
        self.assertTrue(rec["next_actions"])
        # 必须显式提醒不得拿文本端点做图片复核
        self.assertIn("不得用于图片复核", rec["note"])

    def test_available_probe_carries_model_health(self):
        health = {"status": "healthy", "model": "mlx-community/Qwen2.5-VL-3B-Instruct-4bit",
                  "warnings": []}
        with mock.patch.object(scene_model, "_probe_endpoint_health",
                               return_value=(True, health)):
            rec = scene_model.probe_vision_endpoint()
        self.assertTrue(rec["available"])
        self.assertEqual(rec["model_health"]["model"],
                         "mlx-community/Qwen2.5-VL-3B-Instruct-4bit")
        self.assertFalse(rec.get("next_actions"))

    def test_text_probe_unavailable_reports_next_actions(self):
        with mock.patch.object(scene_model, "_probe_endpoint_health",
                               return_value=(False, {})):
            rec = scene_model.probe_text_endpoint()
        self.assertFalse(rec["available"])
        self.assertTrue(rec["next_actions"])

    def test_vision_candidates_match_the_preflight_order(self):
        # 诊断口径必须与真正预检一致，否则"doctor 说好、run 仍失败"
        order = []
        orig = scene_model._probe_endpoint_health

        def spy(endpoint, timeout_s=3.0):
            order.append(scene_model._endpoint_authority(endpoint))
            return orig(endpoint, timeout_s)

        with mock.patch.object(scene_model, "_probe_endpoint_health", side_effect=spy):
            rec = scene_model.probe_vision_endpoint()
        # doctor 探测的端点就是 candidates 里被选中的那一个
        self.assertEqual(order, [rec["endpoint"]])
        self.assertIn(rec["endpoint"], rec["candidates"])


if __name__ == "__main__":
    unittest.main()
