"""材料生成模块定位（U8）：唯一加载点的顺序、覆盖与缺件诊断。

缺陷背景（2026-11-04 载荷审计）：`cli.cmd_from_material` 曾手写两条候选路径，
第一条 `wengao_ppt/material_flow.py` 从未发布（死分支），第二条仅在开发布局成立；
缺件时输出的错误也不含"找过哪些路径"，安装后无法诊断。
本测试钉住新定位器的三个行为：主路径命中、显式覆盖优先、缺件时列出全部尝试路径。
"""
import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from wengao_ppt import material_flow


class MaterialFlowLocatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mf-loc-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop(material_flow.OVERRIDE_ENV, None)
        os.environ.pop(material_flow.DATA_ROOT_ENV, None)

    def test_resolves_the_packaged_implementation(self):
        p = material_flow.resolve()
        self.assertTrue(p.is_file())
        self.assertEqual(p.name, "r5_material_to_ppt.py")
        # 必须落在程序根的 samples/ 内，而不是某个偶然命中
        self.assertEqual(p.parent.name, "samples")

    def test_env_override_wins_over_package_copy(self):
        copy = self.tmp / "elsewhere.py"
        shutil.copy2(material_flow.resolve(), copy)
        os.environ[material_flow.OVERRIDE_ENV] = str(copy)
        self.assertEqual(material_flow.resolve(), copy.resolve())
        # 顺序证据：第一个候选就是覆盖值
        self.assertEqual(material_flow.candidate_paths()[0], copy)

    def test_missing_lists_every_searched_path(self):
        """主路径不存在时必须抛错，并带上**每一处**尝试过的绝对路径。"""
        fake_root = self.tmp / "no-such-program"
        fake_data = self.tmp / "no-such-data"
        os.environ[material_flow.DATA_ROOT_ENV] = str(fake_data)
        os.environ[material_flow.OVERRIDE_ENV] = str(self.tmp / "nope.py")
        with mock.patch.object(material_flow, "package_root", lambda: fake_root):
            with self.assertRaises(material_flow.MaterialFlowMissing) as ctx:
                material_flow.resolve([self.tmp / "extra"])
        searched = ctx.exception.searched
        self.assertEqual(len(searched), 4, searched)
        for frag in ("nope.py", "no-such-program", "no-such-data", "extra"):
            self.assertTrue(any(frag in s for s in searched), (frag, searched))

    def test_candidates_are_deduplicated_and_ordered(self):
        os.environ[material_flow.OVERRIDE_ENV] = str(
            material_flow.package_root() / material_flow.MODULE_RELPATH)
        cands = [str(c) for c in material_flow.candidate_paths(
            [material_flow.package_root()])]
        self.assertEqual(len(cands), len(set(cands)))
        self.assertEqual(cands[0], str(material_flow.package_root()
                                        / material_flow.MODULE_RELPATH))

    def test_load_returns_a_module_with_main(self):
        mod = material_flow.load()
        self.assertTrue(callable(getattr(mod, "main", None)))
        # 模块名固定，保证再次加载时行为一致
        self.assertEqual(mod.__name__, material_flow.MODULE_NAME)


class CliUsesTheSingleLocatorTests(unittest.TestCase):
    def test_cli_delegates_to_material_flow(self):
        """cli 不得再自己拼路径：它必须 import 这个模块。"""
        from wengao_ppt import cli
        src = Path(cli.__file__).read_text(encoding="utf-8")
        self.assertIn("from . import material_flow", src)
        self.assertIn("material_flow.resolve(", src)
        # 旧的死分支写法不得回归
        self.assertNotIn('here / "material_flow.py"', src)


if __name__ == "__main__":
    unittest.main()
