"""视觉升级主题的素材缺失必须**说出来**，而不是抛 FileNotFoundError。

背景（2026-11-04）：载荷按许可政策**故意**不带 `assets/visual_img/`（12 MB，许可结论
未定）。`visual_plan.build_plan` 里原本直接 `Path(lib["file"]).read_bytes()`，于是安装
实例上选到声明 `visual_upgrade` 的主题时，用户看到的是一句
`VISUAL.UPGRADE_FAILED: FileNotFoundError: [Errno 2] ...`——既不说明缺什么，也不说明
为什么缺、能怎么办。

现在改成开跑前先 `require_assets()`：缺素材就是缺素材，用专门的错误码与可操作文案。
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wengao_ppt import visual_plan  # noqa: E402


class MissingAssetsTests(unittest.TestCase):
    def test_dev_tree_has_every_library_file(self):
        self.assertEqual(visual_plan.missing_assets(), [])

    def test_empty_assets_dir_lists_every_library_file(self):
        tmp = Path(tempfile.mkdtemp(prefix="vp-assets-"))
        try:
            miss = visual_plan.missing_assets(tmp)
            self.assertEqual(len(miss), len(visual_plan.LIBRARY))
            self.assertTrue(all(m.endswith(".png") for m in miss))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_partial_assets_report_only_the_missing_ones(self):
        tmp = Path(tempfile.mkdtemp(prefix="vp-assets-partial-"))
        try:
            first = sorted(Path(v["file"]).name for v in visual_plan.LIBRARY.values())[0]
            (tmp / first).write_bytes(b"\x89PNG\r\n\x1a\n")
            miss = visual_plan.missing_assets(tmp)
            self.assertNotIn(first, miss)
            self.assertEqual(len(miss), len(visual_plan.LIBRARY) - 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class RequireAssetsTests(unittest.TestCase):
    def test_error_names_the_cause_and_the_way_out(self):
        tmp = Path(tempfile.mkdtemp(prefix="vp-require-"))
        try:
            with mock.patch.object(visual_plan, "ASSETS", tmp):
                with self.assertRaises(visual_plan.VisualAssetsMissing) as ctx:
                    visual_plan.require_assets()
            msg = str(ctx.exception)
            self.assertIn("assets/visual_img", msg)          # 缺的是什么
            self.assertIn("非视觉主题", msg)                  # 现在能怎么办
            self.assertIn("--include-unverified-assets", msg)  # 另一条路
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_build_plan_checks_assets_before_touching_the_pptx(self):
        """守卫必须在读稿之前：否则错误会伪装成"文件打不开"。"""
        tmp = Path(tempfile.mkdtemp(prefix="vp-order-"))
        try:
            with mock.patch.object(visual_plan, "ASSETS", tmp):
                with self.assertRaises(visual_plan.VisualAssetsMissing):
                    visual_plan.build_plan(str(tmp / "does-not-exist.pptx"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_runner_maps_it_to_a_dedicated_error_code(self):
        # 回归绊线：别再退回"generic VISUAL.UPGRADE_FAILED + FileNotFoundError"
        src = (ROOT / "wengao_ppt" / "runner.py").read_text("utf-8")
        self.assertIn("VISUAL.ASSETS_MISSING", src)
        self.assertIn("vplan_mod.VisualAssetsMissing", src)


if __name__ == "__main__":
    unittest.main()
