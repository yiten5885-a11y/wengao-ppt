"""安装载荷必须带齐**运行期真的会打开**的成员。

背景（2026-11-04 干净安装实测发现的真实缺陷）
---------------------------------------------
`wengao_ppt/runner.py` 用 `program_root / "scripts" / "layout_gate.py"` 打开 HG01–HG12
排版硬门禁的实现，但载荷政策只把 `scripts/` 当"开发脚本"整体排除。后果在安装实例上
同时出现两种：

1. `layout.hard_gate` 恒为 `NOT_EVALUABLE` → `not_evaluable` 非空 → validate 永久阻断，
   任何干净安装都跑不完一个任务；
2. 同一文件还承担原生实播录像 / 独立复核记录的证据校验，缺失时原代码是
   `if gate_path.is_file():` —— **静默跳过**，等于让未校验的录像进门（失败开放）。

所以这里守的是一条结构性不变量：**`install.REQUIRED` 里的每一个成员，都必须能被
载荷生成器的政策产出来**。这两张表历史上是各自手写的，正是它们不一致才漏了文件。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_install_payload as payload  # noqa: E402
from wengao_ppt import install  # noqa: E402


def producible(rel: str) -> bool:
    """载荷政策能否产出这个相对路径（与生成器的包含/排除口径一致）。"""
    pure = Path(rel)
    if any(part in payload.EXCLUDE_DIRS for part in pure.parts[:-1]):
        return False
    if pure.name in payload.EXCLUDE_FILES:
        return False
    if pure.suffix in payload.EXCLUDE_SUFFIXES:
        return False
    if rel in payload.INCLUDE_FILES:
        return True
    for tree in payload.INCLUDE_TREES:
        if rel == tree or rel.startswith(tree + "/"):
            return True
    return False


class RequiredMembersAreShippableTests(unittest.TestCase):
    def test_every_required_member_is_producible(self):
        missing = [r for r in install.REQUIRED if not producible(r)]
        self.assertFalse(
            missing,
            "这些 install.REQUIRED 成员无法被载荷政策产出（安装实例会缺件）：\n  "
            + "\n  ".join(missing))

    def test_layout_gate_runtime_file_is_shipped(self):
        """HG01–HG12 的运行期实现必须随载荷安装，且与 runner 的取址一致。"""
        rel = "scripts/layout_gate.py"
        self.assertIn(rel, install.REQUIRED)
        self.assertIn(rel, payload.INCLUDE_FILES)
        self.assertTrue((ROOT / rel).is_file(), "仓库里根本没有这个文件")
        runner_src = (ROOT / "wengao_ppt" / "runner.py").read_text("utf-8")
        # 取址方式：program_root / "scripts" / "layout_gate.py"
        self.assertIn('program_root / "scripts" / "layout_gate.py"', runner_src)
        for code in ("EVIDENCE.GATE_IMPLEMENTATION_MISSING",
                     "EVIDENCE.GATE_IMPLEMENTATION_UNLOADABLE"):
            self.assertIn(code, runner_src,
                          f"{code} 是「缺实现必须报错」的绊线，不能被删掉")

    def test_frozen_layout_policy_is_shipped(self):
        rel = "contracts/layout_hard_gate_policy.json"
        self.assertIn(rel, install.REQUIRED)
        self.assertTrue(producible(rel))
        # runner 会校验该文件的冻结哈希；换文件必须同步更新常量
        from wengao_ppt import runner
        import hashlib
        actual = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
        self.assertEqual(actual, runner.LAYOUT_GATE_POLICY_BUNDLE_SHA256)


if __name__ == "__main__":
    unittest.main()
