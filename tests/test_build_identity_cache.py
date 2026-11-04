"""install.build_id 的进程内记忆化契约（2026-11-04，install-cleanroom 引出）。

背景：工作台每次 bootstrap 都经 `_instance_info()` 全树重算构建身份
（开发树 15,649 个文件逐个 SHA-256），文件 I/O 受代理的环境里把首屏拖到
35s。构建身份是程序树的确定性聚合、程序运行期不自我改写，因此按目录
记忆化既安全，又把"连续两次只读访问得到同一身份"从巧合变成契约。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wengao_ppt.install import build_id


def _make_tree(root: Path, *, marker: str = "v1") -> None:
    (root / "wengao_ppt").mkdir(parents=True)
    (root / "wengao_ppt" / "__init__.py").write_text(f"__version__ = '{marker}'\n",
                                                     encoding="utf-8")
    (root / "wengao_ppt" / "cli.py").write_text("# cli\n", encoding="utf-8")
    (root / "contracts").mkdir()
    (root / "contracts" / "schemas_v1.json").write_text("{}", encoding="utf-8")


class BuildIdentityMemoizationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wengao-buildid-")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_build_id_is_stable_across_repeated_readonly_calls(self):
        tree = self.root / "tree-a"
        _make_tree(tree)
        first = build_id(tree)
        self.assertTrue(first)
        self.assertEqual(first, build_id(tree), "同一棵树连续两次只读访问必须同一身份")

    def test_memoized_value_survives_unobserved_mutation_until_refresh(self):
        tree = self.root / "tree-b"
        _make_tree(tree)
        before = build_id(tree)
        # 进程内改树：记忆化的语义是"未要求刷新时，读到的是本进程已确立的身份"。
        (tree / "wengao_ppt" / "__init__.py").write_text("__version__ = 'v2'\n",
                                                         encoding="utf-8")
        self.assertEqual(build_id(tree), before)
        refreshed = build_id(tree, refresh=True)
        self.assertNotEqual(refreshed, before, "refresh=True 必须重算并反映改动")
        self.assertEqual(build_id(tree), refreshed, "重算后记忆更新为新身份")

    def test_cache_is_keyed_per_directory(self):
        tree_a = self.root / "tree-c1"
        tree_b = self.root / "tree-c2"
        _make_tree(tree_a)
        _make_tree(tree_b, marker="other")
        id_a = build_id(tree_a)
        id_b = build_id(tree_b)
        self.assertNotEqual(id_a, id_b)
        # 刷新 A 不影响 B 的既有记忆。
        (tree_a / "extra.txt").write_text("x", encoding="utf-8")
        build_id(tree_a, refresh=True)
        self.assertEqual(build_id(tree_b), id_b)

    def test_identity_excludes_runtime_and_data_members(self):
        tree = self.root / "tree-d"
        _make_tree(tree)
        baseline = build_id(tree)
        # 数据根/运行期文件不属于构建身份（install.py DATA_PREFIXES/RUNTIME_SUFFIXES）。
        (tree / "jobs").mkdir()
        (tree / "jobs" / "job-x").write_text("data", encoding="utf-8")
        (tree / "server.log").write_text("log", encoding="utf-8")
        (tree / "__pycache__").mkdir()
        (tree / "__pycache__" / "cli.cpython-313.pyc").write_bytes(b"\x00")
        self.assertEqual(build_id(tree, refresh=True), baseline,
                         "数据/运行期成员不得进入构建身份")


if __name__ == "__main__":
    unittest.main()
