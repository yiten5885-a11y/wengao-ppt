"""共享基础：哈希、JSON 原子写、source_ref、提取结果结构。"""
from __future__ import annotations

import hashlib
import json
import os
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def save_json_atomic(obj, path) -> None:
    """原子写 JSON；目录不存在时创建。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp-" + os.urandom(4).hex())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


PML_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"


def shape_hidden(shp) -> bool:
    """形状是否被显式隐藏。

    cNvPr 位于 presentationml 命名空间（p:cNvPr），不是 drawingml。
    曾因命名空间写错导致隐藏检测从未生效，被 E05 诱饵用例实测抓出。
    同时对 graphicFrame / pic / grpSp 逐一尝试，不依赖单一容器名。
    """
    try:
        el = shp._element
        for ns in (PML_NS, "http://schemas.openxmlformats.org/drawingml/2006/main"):
            for tag in ("cNvPr", "nvSpPr", "nvGraphicFramePr", "nvPicPr", "nvGrpSpPr"):
                for e in el.iter(f"{{{ns}}}{tag}"):
                    if tag == "cNvPr" and e.get("hidden") == "1":
                        return True
                    for c in e.iter(f"{{{ns}}}cNvPr"):
                        if c.get("hidden") == "1":
                            return True
    except Exception:  # noqa: BLE001
        return False
    return False


def shape_visible(shp) -> bool:
    """可见性：未显式隐藏，且尺寸非零。"""
    if shape_hidden(shp):
        return False
    try:
        if shp.width is not None and shp.height is not None and (shp.width <= 0 or shp.height <= 0):
            return False
    except Exception:  # noqa: BLE001
        return True
    return True


def conservative_norm(text: str) -> str:
    """保守规范化：仅 NFC + 保留全部有意义的符号与空白结构。

    明确不做：剥离行首减号、删除星号/指数符号、压掉所有空白、删除引号差异。
    比较用文本与原始文本同时保留。
    """
    if text is None:
        return ""
    t = unicodedata.normalize("NFC", str(text))
    return t


def leading_sign(text: str):
    """返回 (符号, 去掉符号后的主体)。仅识别数值单元行首的 +/-。"""
    t = conservative_norm(text).strip()
    if t.startswith("-"):
        return "-", t[1:].strip()
    if t.startswith("+"):
        return "+", t[1:].strip()
    return "", t


def numeric_tokens(text: str):
    """抽取带符号的数值 token（保留负号），用于符号保真检查。"""
    import re
    t = conservative_norm(text)
    return re.findall(r"(?<![\w.])[+-]?\d+(?:[.,]\d+)*%?", t)


@dataclass
class TextUnit:
    """一个独立文本单元及其来源定位。"""
    id: str
    text: str
    source_ref: dict
    role: str = "body"           # title / body / list / note / table_cell / heading
    in_image: bool = False       # 文字是否来自图片（不得据此降级为 WARN）
    locked: bool = False         # 逐字锁定
    required: bool = True        # 是否必讲

    def norm(self) -> str:
        return conservative_norm(self.text)


@dataclass
class ExtractedDoc:
    """输入材料的独立提取结果（在任何转换之前保存）。"""
    input_path: str
    input_sha256: str
    input_kind: str
    pages: list = field(default_factory=list)        # [{page_no, title, units:[TextUnit], notes}]
    objects: list = field(default_factory=list)      # [{object_id, type, page, path, group_path, visible}]
    tables: list = field(default_factory=list)       # P2：规则表结构模型（dict 形态）
    charts: list = field(default_factory=list)       # P2：原生图表模型（dict 形态，含缓存与工作簿）
    facts: list = field(default_factory=list)        # 主体—值绑定（P1 只登记文本级，表格/图表在 P2）
    heading_index: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["pages"] = [{**p, "units": [asdict(u) if isinstance(u, TextUnit) else u for u in p["units"]]}
                      for p in self.pages]
        return d

    def all_units(self):
        for p in self.pages:
            for u in p["units"]:
                yield p, u
