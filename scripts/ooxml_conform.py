#!/usr/bin/env python3
"""OOXML 结构合格性检查（PPTX）。

定位（必须说清楚）
------------------
本脚本**不是** PowerPoint 兼容性验收。本机没有 PowerPoint，任何"在 PowerPoint
里打开好看/不报修复"的结论都无从得出。本脚本只回答一个更窄、但可执行的问题：
**这个 .pptx 的 OPC/OOXML 结构是否自洽**——部件、内容类型、关系、幻灯片清单、
形状 ID 是否互相对得上。结构不自洽的文件，任何阅读器都可能弹"需要修复"；
结构自洽只是必要条件，不是兼容性证明。

检查项（每项独立计分，失败即整体 FAIL）
--------------------------------------
C1  ZIP 可读、无重复条目、无截断
C2  `[Content_Types].xml` 存在且可解析；Default/Override 覆盖全部部件；
    Override 指向的部件必须存在
C3  每个 `.rels` 可解析、rId 在文件内唯一；内部目标必须指到真实部件
C4  `presentation.xml` 存在；sldIdLst 条目数 == `slides/slideN.xml` 实数，
    且每条 sldId 的 r:id 都能在 `presentation.xml.rels` 里解析到幻灯片
C5  每张 slide 可解析；同一页内 `p:cNvPr/@id` 唯一；`p:cNvPr/@id` 为正整数
C6  每张 slide 的 rels 必须指向存在的 layout；layout 必须指向存在的 master
C7  关系里不得出现 `file:///` 本机绝对路径目标（既不可移植，也会泄漏路径）

用法
----
    python3 scripts/ooxml_conform.py --pptx dist/x.pptx [--json-out out.json]
退出码：0 = 全部 PASS；2 = 存在 FAIL 项；4 = 文件不可读。
"""
from __future__ import annotations

import argparse
import json
import posixpath
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

NS = {
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}

REL_NS = NS["rel"]
SLIDE_RE = re.compile(r"^ppt/slides/slide\d+\.xml$")
LAYOUT_RE = re.compile(r"^ppt/slideLayouts/slideLayout\d+\.xml$")
MASTER_RE = re.compile(r"^ppt/slideMasters/slideMaster\d+\.xml$")


def _norm_part(base_part: str, target: str) -> str:
    """把关系目标解析成包内部件路径（处理 ../ 与绝对 / 形式）。"""
    if target.startswith("/"):
        return target.lstrip("/")
    base_dir = posixpath.dirname(base_part)
    return posixpath.normpath(posixpath.join(base_dir, target))


def _ext(part: str) -> str:
    """部件扩展名（小写）。

    不能用 `posixpath.splitext`：它对**点开头**的名字（`_rels/.rels`）返回空扩展，
    于是把合规的 `.rels` 部件误报成"未声明内容类型"。这里按最后一个点取，
    且把 `.rels` 这种"点即正文"的情形显式当扩展处理。
    """
    name = posixpath.basename(part)
    if name.startswith(".") and name.count(".") == 1:
        return name[1:].lower()
    idx = name.rfind(".")
    return name[idx + 1:].lower() if idx > 0 else ""


def _rels_path_for(part: str) -> str:
    d, name = posixpath.split(part)
    return posixpath.join(d, "_rels", name + ".rels")


class Checker:
    def __init__(self, path: Path):
        self.path = path
        self.results: list[dict] = []
        self.parts: set[str] = set()
        self.zf: zipfile.ZipFile | None = None

    # ---------------- 基础设施 ----------------
    def add(self, code, ok, detail, findings=None):
        self.results.append({"code": code, "ok": bool(ok), "detail": detail,
                             "findings": findings or []})

    def read(self, name: str) -> bytes:
        assert self.zf is not None
        return self.zf.read(name)

    def parse(self, name: str):
        return ET.fromstring(self.read(name))

    def rels_of(self, part: str) -> list[dict]:
        rp = _rels_path_for(part)
        if rp not in self.parts:
            return []
        root = self.parse(rp)
        out = []
        for rel in root.findall(f"{{{REL_NS}}}Relationship"):
            out.append({"id": rel.get("Id"), "type": rel.get("Type", ""),
                        "target": rel.get("Target", ""),
                        "mode": rel.get("TargetMode", "Internal"),
                        "rels_path": rp})
        return out

    # ---------------- 各项检查 ----------------
    def c1_zip(self):
        try:
            self.zf = zipfile.ZipFile(self.path)
        except (zipfile.BadZipFile, OSError) as exc:
            self.add("C1_ZIP_READABLE", False, f"无法作为 ZIP 打开：{exc}")
            return
        names = self.zf.namelist()
        dupes = sorted({n for n in names if names.count(n) > 1})
        bad = self.zf.testzip()
        self.parts = {n for n in names if not n.endswith("/")}
        self.add("C1_ZIP_READABLE", bad is None and not dupes,
                 f"{len(names)} 条目；损坏成员={bad}；重复条目={dupes[:5]}")

    def c2_content_types(self):
        ct = "[Content_Types].xml"
        if ct not in self.parts:
            self.add("C2_CONTENT_TYPES", False, "缺 [Content_Types].xml")
            return
        try:
            root = self.parse(ct)
        except ET.ParseError as exc:
            self.add("C2_CONTENT_TYPES", False, f"[Content_Types].xml 解析失败：{exc}")
            return
        defaults = {d.get("Extension", "").lower()
                    for d in root.findall(f"{{{NS['ct']}}}Default")}
        overrides = {o.get("PartName", "").lstrip("/")
                     for o in root.findall(f"{{{NS['ct']}}}Override")}
        findings = []
        for p in sorted(self.parts):
            if p == ct:
                continue
            if p in overrides:
                continue
            ext = _ext(p)
            if ext and ext in defaults:
                continue
            findings.append({"part": p, "why": "既无 Override，扩展名也未在 Default 中声明"})
        dangling = sorted(overrides - self.parts)
        for d in dangling:
            findings.append({"part": d, "why": "Override 指向不存在的部件"})
        self.add("C2_CONTENT_TYPES", not findings,
                 f"Default={len(defaults)} Override={len(overrides)}；"
                 f"问题 {len(findings)} 项", findings)

    def c3_relationships(self):
        findings = []
        rels_parts = [p for p in self.parts if p.endswith(".rels")]
        for rp in sorted(rels_parts):
            try:
                root = self.parse(rp)
            except ET.ParseError as exc:
                findings.append({"part": rp, "why": f"rels 解析失败：{exc}"})
                continue
            seen = set()
            for rel in root.findall(f"{{{REL_NS}}}Relationship"):
                rid = rel.get("Id")
                if rid in seen:
                    findings.append({"part": rp, "why": f"rId 重复：{rid}"})
                seen.add(rid)
                target = rel.get("Target", "")
                # `file:` 目标即使是 External 也要报：它把本机绝对路径写进了文档，
                # 换台机器必然断链，且构成路径泄漏。
                if target.startswith("file:"):
                    findings.append({"part": rp, "why": f"本机 file: 目标：{target}"})
                    continue
                if rel.get("TargetMode") == "External":
                    continue
                # rels 自身所在部件决定相对基准
                owner_dir = posixpath.dirname(posixpath.dirname(rp))
                resolved = posixpath.normpath(posixpath.join(owner_dir, target))
                if resolved not in self.parts:
                    findings.append({"part": rp, "why": f"目标不存在：{target} -> {resolved}"})
        self.add("C3_RELATIONSHIPS", not findings,
                 f"检查 {len(rels_parts)} 个 rels；问题 {len(findings)} 项", findings)

    def c4_slide_list(self):
        pres = "ppt/presentation.xml"
        if pres not in self.parts:
            self.add("C4_SLIDE_LIST", False, "缺 ppt/presentation.xml")
            return
        try:
            root = self.parse(pres)
        except ET.ParseError as exc:
            self.add("C4_SLIDE_LIST", False, f"presentation.xml 解析失败：{exc}")
            return
        sld_ids = root.findall(f"{{{NS['p']}}}sldIdLst/{{{NS['p']}}}sldId")
        slide_parts = sorted(p for p in self.parts if SLIDE_RE.match(p))
        rels = {r["id"]: r for r in self.rels_of(pres)}
        findings = []
        if len(sld_ids) != len(slide_parts):
            findings.append({"why": f"sldIdLst={len(sld_ids)} 与实际幻灯片 "
                                     f"{len(slide_parts)} 不一致"})
        for s in sld_ids:
            rid = s.get(f"{{{NS['r']}}}id")
            rel = rels.get(rid)
            if rel is None:
                findings.append({"why": f"sldId r:id={rid} 在 presentation.xml.rels 中不存在"})
            elif _norm_part(pres, rel["target"]) not in self.parts:
                findings.append({"why": f"sldId r:id={rid} 目标部件不存在：{rel['target']}"})
        self.add("C4_SLIDE_LIST", not findings,
                 f"sldIdLst={len(sld_ids)} slides={len(slide_parts)}；"
                 f"问题 {len(findings)} 项", findings)

    def c5_shape_ids(self):
        findings = []
        slides = sorted(p for p in self.parts if SLIDE_RE.match(p))
        for sp in slides:
            try:
                root = self.parse(sp)
            except ET.ParseError as exc:
                findings.append({"part": sp, "why": f"幻灯片解析失败：{exc}"})
                continue
            ids = []
            for cnv in root.iter(f"{{{NS['p']}}}cNvPr"):
                raw = cnv.get("id")
                try:
                    ids.append(int(raw))
                except (TypeError, ValueError):
                    findings.append({"part": sp, "why": f"非法 shape id：{raw!r}"})
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            if dupes:
                findings.append({"part": sp, "why": f"shape id 重复：{dupes[:5]}"})
            if any(i < 1 for i in ids):
                findings.append({"part": sp, "why": "存在 id<1 的形状（PowerPoint 视为损坏）"})
        self.add("C5_SHAPE_IDS", not findings,
                 f"检查 {len(slides)} 页；问题 {len(findings)} 项", findings)

    def c6_layout_master_chain(self):
        findings = []
        for sp in sorted(p for p in self.parts if SLIDE_RE.match(p)):
            rels = self.rels_of(sp)
            lay = [r for r in rels if r["type"].endswith("/slideLayout")]
            if not lay:
                findings.append({"part": sp, "why": "缺 slideLayout 关系"})
                continue
            lp = _norm_part(sp, lay[0]["target"])
            if lp not in self.parts:
                findings.append({"part": sp, "why": f"layout 不存在：{lp}"})
                continue
            mrels = [r for r in self.rels_of(lp)
                     if r["type"].endswith("/slideMaster")]
            if not mrels:
                findings.append({"part": lp, "why": "layout 缺 slideMaster 关系"})
            elif _norm_part(lp, mrels[0]["target"]) not in self.parts:
                findings.append({"part": lp, "why": "master 不存在"})
        orphans = [p for p in self.parts if LAYOUT_RE.match(p)]
        if not orphans:
            findings.append({"why": "包内没有任何 slideLayout"})
        if not [p for p in self.parts if MASTER_RE.match(p)]:
            findings.append({"why": "包内没有任何 slideMaster"})
        self.add("C6_LAYOUT_MASTER_CHAIN", not findings,
                 f"layout={len([p for p in self.parts if LAYOUT_RE.match(p)])}；"
                 f"问题 {len(findings)} 项", findings)

    def c7_no_local_uris(self):
        findings = []
        for rp in sorted(p for p in self.parts if p.endswith(".rels")):
            text = self.read(rp).decode("utf-8", "replace")
            for m in re.finditer(r"file:///Users/[^\"']*", text):
                findings.append({"part": rp, "why": m.group(0)[:120]})
        self.add("C7_NO_LOCAL_FILE_URIS", not findings,
                 f"问题 {len(findings)} 项", findings)

    # ---------------- 编排 ----------------
    def run(self) -> dict:
        self.c1_zip()
        if self.zf is None:
            return self.report()
        for fn in (self.c2_content_types, self.c3_relationships, self.c4_slide_list,
                   self.c5_shape_ids, self.c6_layout_master_chain, self.c7_no_local_uris):
            fn()
        self.zf.close()
        return self.report()

    def report(self) -> dict:
        failed = [r for r in self.results if not r["ok"]]
        return {
            "schema": "wengao.ooxml_conform.v1",
            "pptx": str(self.path),
            "bytes": self.path.stat().st_size if self.path.is_file() else None,
            "scope": "OPC/OOXML 结构自洽性；**不是** PowerPoint 兼容性验收",
            "checks": self.results,
            "passed": len(self.results) - len(failed),
            "failed": len(failed),
            "verdict": "PASS" if not failed else "FAIL",
        }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="PPTX 的 OPC/OOXML 结构合格性检查")
    ap.add_argument("--pptx", required=True)
    ap.add_argument("--json-out", default=None)
    a = ap.parse_args(argv)
    p = Path(a.pptx).expanduser().resolve()
    if not p.is_file():
        print(json.dumps({"ok": False, "detail": f"文件不存在：{p}"},
                         ensure_ascii=False, indent=2))
        return 4
    rep = Checker(p).run()
    if a.json_out:
        out = Path(a.json_out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: rep[k] for k in ("pptx", "verdict", "passed", "failed")},
                     ensure_ascii=False, indent=2))
    for r in rep["checks"]:
        print(f"[{'PASS' if r['ok'] else 'FAIL'}] {r['code']} — {r['detail']}",
              file=sys.stderr)
        for f in r["findings"][:5]:
            print(f"    - {f}", file=sys.stderr)
    return 0 if rep["verdict"] == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
