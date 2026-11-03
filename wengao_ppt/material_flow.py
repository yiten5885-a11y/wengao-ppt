"""材料生成链路（`from-material`）的**唯一**定位与加载点。

为什么要有这个模块
------------------
`cli.cmd_from_material` 原先在手写两条候选路径：`wengao_ppt/material_flow.py`
（该文件从未存在）与 `samples/r5_material_to_ppt.py`。前者是**死分支**，
后者只在"包根旁边正好有 samples/"时成立——安装布局、自定义根、被裁剪的
载荷都会静默落到 `GATE.MATERIAL_PATH_MISSING`，而错误信息不告诉你找过哪些
位置（2026-11-04 载荷审计：U8）。

本模块把定位逻辑收成一处：显式搜索顺序、可被环境变量覆盖、失败时把
**逐个尝试过的绝对路径**带出去。实现本体仍是 `samples/r5_material_to_ppt.py`
（3.2k 行，单一事实源），不复制第二份，避免两份代码漂移。

搜索顺序（先命中先用）
----------------------
1. `$WENGAO_MATERIAL_FLOW`（显式覆盖，必须是文件；用于诊断与特殊布局）
2. `<程序根>/samples/r5_material_to_ppt.py`（开发布局与安装布局的主路径）
3. `<数据根>/samples/r5_material_to_ppt.py`（用户把链路放进数据根时）
4. 调用方额外给的候选根（`extra_roots`，按顺序）

边界：本模块只负责**找到并加载**，不做任何能力判断；脚本存在但内容损坏时
由加载异常原样抛出，不吞掉。
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

MODULE_NAME = "r5_material_to_ppt"
MODULE_RELPATH = Path("samples") / "r5_material_to_ppt.py"
OVERRIDE_ENV = "WENGAO_MATERIAL_FLOW"
DATA_ROOT_ENV = "WENGAO_DATA_ROOT"


class MaterialFlowMissing(RuntimeError):
    """材料生成模块在所有候选位置都不存在。

    `searched` 是**实际尝试过的绝对路径列表**，供调用方原样输出，
    避免"缺失但不知道找过哪里"这类不可诊断失败。
    """

    def __init__(self, searched: list[str]):
        self.searched = list(searched)
        super().__init__(
            "材料生成模块缺失；已尝试：" + "；".join(self.searched))


def package_root() -> Path:
    """程序根（含 `wengao_ppt/`、`samples/`、`contracts/` 的那一层）。"""
    return Path(__file__).resolve().parents[1]


def _data_root() -> Path | None:
    raw = os.environ.get(DATA_ROOT_ENV)
    if raw:
        try:
            return Path(raw).expanduser().resolve()
        except OSError:
            return None
    return None


def candidate_paths(extra_roots=()) -> list[Path]:
    """按搜索顺序返回候选文件路径（可能重复，保持顺序）。"""
    cands: list[Path] = []
    override = os.environ.get(OVERRIDE_ENV)
    if override:
        cands.append(Path(override).expanduser())
    cands.append(package_root() / MODULE_RELPATH)
    dr = _data_root()
    if dr is not None:
        cands.append(dr / MODULE_RELPATH)
    for r in extra_roots:
        cands.append(Path(r).expanduser() / MODULE_RELPATH)
    seen, out = set(), []
    for c in cands:
        try:
            key = str(c.resolve())
        except OSError:
            key = str(c)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def resolve(extra_roots=()) -> Path:
    """返回第一个真实存在的材料模块路径；都不存在时抛 `MaterialFlowMissing`。"""
    cands = candidate_paths(extra_roots)
    for c in cands:
        if c.is_file():
            return c.resolve()
    raise MaterialFlowMissing([str(c) for c in cands])


def load(extra_roots=()):
    """加载并返回材料模块对象（`main()` 即入口）。"""
    path = resolve(extra_roots)
    spec = importlib.util.spec_from_file_location(MODULE_NAME, path)
    if spec is None or spec.loader is None:  # pragma: no cover - 极端环境
        raise MaterialFlowMissing([f"{path}（无法构造加载器）"])
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
