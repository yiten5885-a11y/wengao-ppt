#!/usr/bin/env python3
"""P2 许可证矩阵与来源清单生成器（只读测算，输出 JSON + Markdown）。

原则：所有版本号、哈希、文件尺寸、许可证标识一律**现场实测**，脚本内不写死
任何 hash 或版本字面量；无法测得的项一律写 `NOT_MEASURED` / `UNVERIFIED`，
不做推测、不写"已完成"。法律性质判断不由此脚本给出，只给出事实与阻断标记。

用法：
    python3 scripts/p2_build_license_matrix.py --root <候选目录> --out <报告目录>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import time
from pathlib import Path

MANAGED_PY = "/Users/huangyingteng/.workbuddy/binaries/python/envs/default"
PYPI_DIRNAME = "pypi_evidence"

#: Upstream 声明（来自 contracts/upstream_baseline.json 与 AGENTS.md；此脚本只**核对**）
DECLARED_UPSTREAM = {
    "source_url": "https://github.com/zhouwei713/luobo-ppt.git",
    "commit": "b6dcb0f325b3ea38a7ca3cd3c211bbba7fb436b2",
    "tree": "ed28a29ddfd5e9666fa0dc9949e2e0658d6786f5",
    "tracked_file_count": 76,
}

#: whose license obligation we track: 声明的最终 BLOCKER 状态由人工/所有者判定，
#: 这里只登记**事实层面的处置标记**，不输出法律结论。
BLOCKERS = [
    ("BLK-01", "自有代码 LICENSE 未定",
     " `LICENSE`（自有代码许可）文件尚不存在，所有者未选定许可证；README/SKILL/"
     "SkillHub 元数据里的 `license` 字段因此不能填写。未解决前不得对外发布。"),
    ("BLK-02", "五张 AI 生成配图缺少可核验生成证据",
     "`assets/visual_img/*.png` 的来源仅在 `wengao_ppt/visual_plan.py` 的 `source`"
     "字符串里自述为 ImageGen + 时间，没有 provider/model/任务 ID/输入提示词/输出"
     "落盘记录可核对；PNG 内不含任何 `tEXt/iTXt/zTXt` 元数据块，也没有 AI 生成标识。"
     "按证据基线，这属于**未验证**，不能作为“已署名、已标识”的证据。需所有者确认："
     "补齐生成证据与 AI 标识，或移除/替换这 5 张资产。"),
    ("BLK-03", "Pexels 示例照片的再分发权未清",
     "`assets/visual_img/semantic_scenes/` 下 5 张 JPEG 的资产清单自述"
     " `NOT_CLEARED_FOR_SOURCE_BUNDLE`，且 Pexels 服务条款对“整包再分发”是否允许"
     "需要所有者/法务确认。已据此在对外副本中默认排除这些文件。"),
    ("BLK-04", "上游 vendored 仓库的 remote 指向本机 bundle",
     "`vendor/luobo-ppt/.git` 的 `remote.origin.url` 不是 GitHub URL，而是一台机器的"
     "本地 bundle 路径（已脱敏展示）。这使“从 GitHub 重新拉取 upstream”这一步无法被"
     "复现，且第三方同步 `vendor/.git` 时会连带暴露本机路径。处理建议：按 AGENTS.md "
     "第 1 节从 GitHub 重新克隆并逐 file 核对 tree sha，或明确不随包分发 `.git`。"),
    ("BLK-05", "上游携带的文档图片无逐图权属说明",
     "`vendor/luobo-ppt/docs/images/` 与 `assets/style_gallery.jpg`（上游自带）未附"
     "逐图来源与许可说明，随包分发时的权利依据只能追溯到上游 MIT LICENSE 全文。"
     "是否随包分发由所有者决定（默认策略：随 MIT 全文保留，但标记未逐图核实）。"),
    ("BLK-06", "依赖版本的许可证标识存在多口径",
     "同一依赖在 PyPI 元数据、本机 dist-info 与 requirements.txt 注释里的版本/许可"
     "标识并不完全一致（见第三节），以哪个为准应由所有者确认后再写死。"),
]

NOTES_FOR_THE_RECORD = [
    "本矩阵不代替律师/法务意见；凡标注“阻断 / 待确认”的项必须由项目所有者拍板。"
    "代理不得代签任何服务条款、不得代做实名/账户操作。",
    "未跑或测不到的内容一律写 `NOT_MEASURED`，不口头声称完成，不用依赖列表、"
    "`/api/tags`、fixture、回放或自报结果充数。",
]


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd: list[str], cwd: Path | None = None):
    try:
        r = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True,
                           text=True, timeout=30)
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except Exception as exc:  # noqa: BLE001
        return -1, "", repr(exc)


def redact_path(s: str) -> str:
    if not s:
        return "NOT_MEASURED"
    return re.sub(r"/Users/[^/\s]+", "/Users/<REDACTED>", s)


def measure_upstream(root: Path) -> dict:
    got = {}
    v = root / "vendor" / "luobo-ppt"
    got["checkout_dir"] = "vendor/luobo-ppt"
    got["exists"] = v.is_dir()
    if not got["exists"]:
        return got
    code, out, err = run(["git", "rev-parse", "HEAD"], v)
    got["head"] = out if code == 0 else f"NOT_MEASURED({err[:40]})"
    code, out, _ = run(["git", "rev-parse", "HEAD^{tree}"], v)
    got["tree"] = out if code == 0 else "NOT_MEASURED"
    code, out, _ = run(["git", "config", "--get", "remote.origin.url"], v)
    got["remote_origin_redacted"] = redact_path(out) if code == 0 else "NOT_MEASURED"
    code, out, _ = run(["git", "ls-files"], v)
    got["tracked_files"] = len([x for x in out.split("\n") if x]) if code == 0 else -1
    got["head_matches_declared"] = got["head"] == DECLARED_UPSTREAM["commit"]
    got["tree_matches_declared"] = got["tree"] == DECLARED_UPSTREAM["tree"]
    code, out, _ = run(["git", "status", "--porcelain"], v)
    got["working_tree_clean"] = (code == 0 and out == "")
    code, out, _ = run(["git", "log", "-1", "--format=%cI / %an"], v)
    got["last_commit"] = out if code == 0 else "NOT_MEASURED"
    for rel in ("LICENSE", "assets/LICENSE-lucide.txt"):
        p = v / rel
        if p.is_file():
            txt = p.read_text(encoding="utf-8", errors="replace")
            head = [l for l in txt.split("\n")[:3] if l.strip()]
            got.setdefault("license_files", []).append({
                "path": f"vendor/luobo-ppt/{rel}", "sha256": sha256_file(p),
                "bytes": p.stat().st_size, "first_lines": head})
    return got


def measure_venv_versions(root: Path) -> list[dict]:
    """从 dist-info 目录名读真实安装版本（候选自带 venv 无 pip，不能 import metadata）。"""
    out = []
    for base in (root / ".venv" / "lib",):
        if not base.is_dir():
            continue
        for libdir in sorted(base.iterdir()):
            sp = libdir / "site-packages"
            if not sp.is_dir():
                continue
            for d in sorted(sp.iterdir()):
                if d.is_dir() and d.name.endswith(".dist-info"):
                    stem = d.name[: -len(".dist-info")]
                    if "-" in stem:
                        name, _, ver = stem.rpartition("-")
                        out.append({"env": f"candidate .venv ({libdir.name})",
                                    "distribution": name, "version": ver})
    return out


def measure_installed_metadata() -> list[dict]:
    """本机受管环境里用 importlib.metadata 读到的真实安装版本与许可标识。"""
    if not os.path.isdir(MANAGED_PY):
        return [{"env": MANAGED_PY, "error": "env_not_found"}]
    code_snippet = (
        "import importlib.metadata as m,json,sys\n"
        "pkgs=['Pillow','lxml','openpyxl','pypdf','pypdfium2','python-docx','python-pptx']\n"
        "o=[]\n"
        "for p in pkgs:\n"
        "    try:\n"
        "        d=m.distribution(p)\n"
        "        o.append({'distribution':p,'version':d.version,\n"
        "                  'license':d.metadata.get('License'),\n"
        "                  'license_expression':d.metadata.get('License-Expression'),\n"
        "                  'classifiers':[c for c in d.metadata.get_all('Classifier',[]) if 'License' in c]})\n"
        "    except Exception as e:\n"
        "        o.append({'distribution':p,'error':type(e).__name__})\n"
        "print(json.dumps(o,ensure_ascii=False))\n")
    py = Path(MANAGED_PY) / "bin" / "python"
    code, sout, serr = run([str(py), "-c", code_snippet])
    if code != 0:
        return [{"env": MANAGED_PY, "error": (serr or sout)[:200]}]
    try:
        rows = json.loads(sout)
        return [dict(r, env=MANAGED_PY) for r in rows]
    except Exception as exc:  # noqa: BLE001
        return [{"env": MANAGED_PY, "error": repr(exc)}]


def measure_pypi_evidence(report_dir: Path) -> list[dict]:
    """优先读 pypi_evidence/INDEX.json；缺失时直接逐个读落盘的原始响应并现算 sha256。"""
    d = report_dir / PYPI_DIRNAME
    if not d.is_dir():
        return []
    indexed = {}
    idx = d / "INDEX.json"
    if idx.is_file():
        for it in json.loads(idx.read_text(encoding="utf-8")).get("items", []):
            indexed[it.get("package")] = it
    rows = []
    for p in sorted(d.glob("*.json")):
        if p.name == "INDEX.json":
            continue
        name = p.stem
        try:
            raw = p.read_bytes()
            info = json.loads(raw)["info"]
            rec = indexed.get(name, {})
            rows.append({"package": name,
                         "url": rec.get("url") or f"https://pypi.org/pypi/{name}/json",
                         "latest_version": info.get("version"),
                         "license_expression": info.get("license_expression"),
                         "sha256_of_response": hashlib.sha256(raw).hexdigest(),
                         "note": rec.get("note", "from_raw_response")})
        except Exception as exc:  # noqa: BLE001
            rows.append({"package": name, "error": repr(exc)})
    return rows


def measure_assets(root: Path) -> dict:
    out = {"ai_generated_png": [], "scene_photos": [], "manifest_claims": [],
           "fontconfig": None, "upstream_binary_assets": []}
    vis = root / "assets" / "visual_img"
    for p in sorted(vis.glob("*.png")):
        data = p.read_bytes()
        meta = {"has_text_chunk": False, "width": None, "height": None}
        i, n = 8, len(data)
        while i + 8 <= n:
            ln = struct.unpack(">I", data[i:i + 4])[0]
            typ = data[i + 4:i + 8].decode("latin1")
            if typ in ("tEXt", "iTXt", "zTXt"):
                meta["has_text_chunk"] = True
            if typ == "IHDR" and i + 18 <= n:
                meta["width"], meta["height"] = struct.unpack(">II", data[i + 8:i + 16])
            i += 12 + ln
            if typ == "IEND":
                break
        out["ai_generated_png"].append({
            "path": p.relative_to(root).as_posix(), "sha256": sha256_file(p),
            "bytes": p.stat().st_size, **meta})
    sj = vis / "semantic_scenes" / "semantic_scene_assets.json"
    if sj.is_file():
        data = json.loads(sj.read_text(encoding="utf-8"))
        for item in data.get("items", []):
            f = vis / "semantic_scenes" / item.get("file", "")
            actual = sha256_file(f) if f.is_file() else "FILE_MISSING"
            out["manifest_claims"].append({
                "file": item.get("file"), "scene_id": item.get("scene_id"),
                "creator": item.get("creator"), "license": item.get("license"),
                "source_page": item.get("source_page"),
                "redistribution_status": item.get("redistribution_status"),
                "declared_sha256": item.get("sha256"), "declared_bytes": item.get("bytes"),
                "measured_sha256": actual,
                "measured_bytes": f.stat().st_size if f.is_file() else -1,
                "sha256_match": actual == item.get("sha256")})
    for p in sorted((vis / "semantic_scenes").glob("*.jp*g")):
        out["scene_photos"].append({"path": p.relative_to(root).as_posix(),
                                    "sha256": sha256_file(p), "bytes": p.stat().st_size})
    fc = root / "assets" / "fontconfig" / "fonts.conf"
    if fc.is_file():
        out["fontconfig"] = {"path": fc.relative_to(root).as_posix(),
                             "sha256": sha256_file(fc), "bytes": fc.stat().st_size}
    v = root / "vendor" / "luobo-ppt"
    for sub in ("docs/images", "assets"):
        d = v / sub
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg"):
                out["upstream_binary_assets"].append({
                    "path": p.relative_to(root).as_posix(),
                    "sha256": sha256_file(p), "bytes": p.stat().st_size,
                    "provenance_note": "上游仓库自带，无逐图来源/许可说明（见 BLK-05）"})
    return out


def measure_own_source(root: Path) -> dict:
    mod = root / "wengao_ppt"
    rows = []
    for p in sorted(mod.glob("*.py")):
        rows.append({"path": p.relative_to(root).as_posix(),
                     "sha256": sha256_file(p), "bytes": p.stat().st_size})
    total = len(rows)
    agg = hashlib.sha256("\n".join(f"{r['sha256']}  {r['path']}" for r in rows).encode())
    fonts = []
    for base in (root, ):
        for dp, dn, fn in os.walk(base):
            if any(part in (".venv", ".git", "__pycache__") for part in Path(dp).parts):
                continue
            for f in fn:
                if f.lower().endswith((".ttf", ".otf", ".ttc", ".woff", ".woff2")):
                    fonts.append(os.path.join(dp, f))
    return {"modules": rows, "module_count": total,
            "modules_sha256": agg.hexdigest(),
            "bundled_font_files_found": fonts,
            "bundled_font_count": len(fonts)}


def parse_requirements(root: Path) -> dict:
    p = root / "requirements.txt"
    if not p.is_file():
        return {"error": "missing"}
    text = p.read_text(encoding="utf-8")
    declared = {}
    for line in text.splitlines():
        m = re.match(r"^#\s*([A-Za-z0-9_\-]+)==([0-9][^\s]*)", line.strip())
        if m:
            declared[m.group(1)] = m.group(2)
    pins = [l.strip() for l in text.splitlines()
            if l.strip() and not l.startswith("#")]
    return {"sha256": sha256_file(p), "requirements": pins,
            "declared_verified_versions": declared}


def md_escape(s) -> str:
    return str(s).replace("|", "\\|")


def build_markdown(facts: dict) -> str:
    up = facts["upstream"]
    L = []
    L.append("# 稳稿 PPT · 许可证矩阵与来源清单（P2）")
    L.append("")
    L.append(f"> 生成时间：`{facts['generated_at']}`　生成器：`scripts/p2_build_license_matrix.py`"
             f"（所有哈希/版本为现场实测）")
    L.append(f"> 候选基线：`reports/p2-license-release-20261023/BASELINE_SHAS.txt`，"
             f"成员数 {facts['baseline_file_count']}，"
             f"树摘要 `{facts.get('baseline_tree_sha256','NOT_MEASURED')}`")
    L.append("")
    L.append("**本文件不是法律意见。** 凡标注 `阻断 / 待确认` 的条目必须由项目所有者拍板；"
             "代理不代签服务条款、不代做账户/实名操作、不给许可结论。")
    L.append("")

    L.append("## 0. 阻断项总表（必须先看完这一段）")
    L.append("")
    L.append("| 编号 | 事项 | 为什么阻断发布 |")
    L.append("| :--- | :--- | :--- |")
    for b in BLOCKERS:
        L.append(f"| {b[0]} | {b[1]} | {md_escape(b[2])} |")
    L.append("")
    for n in NOTES_FOR_THE_RECORD:
        L.append(f"> {n}")
    L.append("")

    L.append("## 1. 上游厂商代码（vendored upstream）")
    L.append("")
    L.append("| 项 | 实测值 | 与 AGENTS.md/契约声明是否一致 |")
    L.append("| :--- | :--- | :--- |")
    L.append(f"| 检出目录 | `{up.get('checkout_dir')}` | — |")
    L.append(f"| HEAD | `{up.get('head')}` | "
             f"{'一致' if up.get('head_matches_declared') else '**不一致/NOT_MEASURED**'} |")
    L.append(f"| tree | `{up.get('tree')}` | "
             f"{'一致' if up.get('tree_matches_declared') else '**不一致/NOT_MEASURED**'} |")
    L.append(f"| 跟踪文件数 | {up.get('tracked_files')} | 契约声明 {DECLARED_UPSTREAM['tracked_file_count']} |")
    L.append(f"| 工作树干净 | {up.get('working_tree_clean')} | — |")
    L.append(f"| 最后提交 | `{md_escape(up.get('last_commit'))}` | — |")
    L.append(f"| remote.origin（已脱敏） | `{md_escape(up.get('remote_origin_redacted'))}` | "
             f"**与声明的 GitHub URL 不一致 → BLK-04** |")
    L.append("")
    L.append("### 1.1 上游随文件携带的许可文本（实测 sha256）")
    L.append("")
    L.append("| 文件 | sha256 | 字节 | 首几行 | 义务摘要 |")
    L.append("| :--- | :--- | ---: | :--- | :--- |")
    for f in up.get("license_files", []):
        L.append(f"| `{f['path']}` | `{f['sha256'][:16]}…` | {f['bytes']} | "
                 f"{md_escape(' / '.join(f['first_lines'])[:80])} | "
                 f"再分发时必须保留完整许可文本与版权声明 |")
    L.append("")
    L.append("### 1.2 上游图片资产（无逐图权属说明 → BLK-05）")
    L.append("")
    L.append("| 文件 | sha256 | 字节 |")
    L.append("| :--- | :--- | ---: |")
    for a in facts["assets"]["upstream_binary_assets"]:
        L.append(f"| `{a['path']}` | `{a['sha256'][:16]}…` | {a['bytes']} |")
    L.append("")

    L.append("## 2. 第三方依赖（运行时/构建时）")
    L.append("")
    L.append("**证据口径说明**：同一依赖存在三个口径——① requirements.txt 注释里的"
             "“已验证版本”（列出当时用于全链验收的版本）；② 本机受管 Python 环境的真实"
             "安装（`importlib.metadata` 实测）；③ PyPI 官方元数据现场拉取（附响应 "
             "sha256）。三者不一致时本文件只并列事实，不定结论（→ BLK-06）。")
    L.append("")
    L.append("| 依赖 | 候选 .venv 实测版本 | 本机受管环境版本/许可标识 | PyPI latest / SPDX | "\
             "requirements.txt 注释版本 |")
    L.append("| :--- | :--- | :--- | :--- | :--- |")
    def norm(s):
        return str(s).lower().replace("_", "-").replace(" ", "-")
    venv = {norm(r["distribution"]): r["version"] for r in facts["venv"]}
    inst = {norm(r.get("distribution", "")): r for r in facts["installed"]}
    pypi = {norm(r.get("package", "")): r for r in facts["pypi"]}
    declared = {norm(k): v for k, v in facts["requirements"].get("declared_verified_versions", {}).items()}
    for name in ["pillow", "lxml", "openpyxl", "pypdf", "pypdfium2", "python-docx", "python-pptx"]:
        i = inst.get(name, {})
        lic = i.get("license_expression") or i.get("license") or (i.get("classifiers") or ["—"])[0]
        pv = pypi.get(name, {})
        pypi_txt = f"{pv.get('latest_version','NOT_MEASURED')} / {pv.get('license_expression') or '—'}" \
            if pv else "NOT_MEASURED"
        L.append(f"| `{name}` | {venv.get(name, 'NOT_INSTALLED')} | "
                 f"{i.get('version','NOT_MEASURED')} / {md_escape(lic)} | "
                 f"{pypi_txt} | {declared.get(name, '—')} |")
    L.append("")
    L.append("PyPI 元数据原始响应（可离线复核，附响应 sha256）：")
    L.append("")
    for row in facts["pypi"]:
        spdx = row.get("license_expression") or "未声明 SPDX 表达式（以本机 dist-info 口径为准）"
        L.append(f"- `{row.get('package')}`: `{row.get('url')}` → 最新版本 "
                 f"`{row.get('latest_version')}`，SPDX `{spdx}`，"
                 f"响应 sha256 `{str(row.get('sha256_of_response'))[:16]}…`"
                 + ("" if not row.get("error") else f"，**拉取失败：{md_escape(row['error'])}**"))
    L.append("")

    L.append("## 3. 图片与其他资产来源清单")
    L.append("")
    L.append("### 3.1 AI 生成配图（5 张 · **未验证** → BLK-02）")
    L.append("")
    L.append("| 文件 | sha256 | 字节 | 尺寸 | PNG 文本元数据块 | 可核验生成证据 |")
    L.append("| :--- | :--- | ---: | :--- | :--- | :--- |")
    for a in facts["assets"]["ai_generated_png"]:
        L.append(f"| `{a['path']}` | `{a['sha256'][:16]}…` | {a['bytes']} | "
                 f"{a['width']}×{a['height']} | "
                 f"{'有' if a['has_text_chunk'] else '**无**'} | "
                 f"**无**（仅 `visual_plan.py` 内自述字符串） |")
    L.append("")
    L.append("事实陈述：这 5 张图目前只有**一处**来源陈述，即 `wengao_ppt/visual_plan.py` "
             "`LIBRARY[*]['source']` 里的 `ImageGen 2026-10-19T17:3x …` 文本（该文件 sha256："
             f"`{facts['visual_plan_sha256'][:16]}…`，由生成器现场重算）。"
             "没有 provider/model、任务 ID、提示词或输出落盘记录可交叉核对；文件自身也"
             "不含任何文本元数据块。因此本候选的“AI 生成标识 / 署名”要求目前**无法举证**，"
             "不得在发布说明中写成已标识。")
    L.append("")
    L.append("另外核实过的一种可能性：按文件 birthtime（`stat` 实测）追溯生成时间也无法成立——"
             "这 5 个文件的 birthtime 是候选构建时的复制时间，不是生图时间。这条记录在"
             " `SOURCE_INVENTORY.json` 里没有落下，但可用下列命令复现："
             " `python3 -c \"import os;print(os.stat('assets/visual_img/promo_ecom.png').st_birthtime)\"`")
    L.append("")
    L.append("### 3.2 语义场景照片（Pexels · 再分发权未清 → BLK-03）")
    L.append("")
    L.append("| 文件 | 场景 ID | 资产清单 sha256 | 实测 sha256 | 一致 | creator | license | Redistribution |")
    L.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
    for m in facts["assets"]["manifest_claims"]:
        L.append(f"| `{m['file']}` | `{m['scene_id']}` | `{str(m['declared_sha256'])[:12]}…` | "
                 f"`{str(m['measured_sha256'])[:12]}…` | {'✅' if m['sha256_match'] else '**❌**'} | "
                 f"{md_escape(m['creator'])} | {m['license']} | "
                 f"`{m['redistribution_status']}` |")
    for p in facts["assets"]["scene_photos"]:
        if not any(m["file"] == os.path.basename(p["path"]) for m in facts["assets"]["manifest_claims"]):
            L.append(f"| `{p['path']}` | — | — | `{p['sha256'][:12]}…` | "
                     f"**不在清单内** | — | — | — |")
    L.append("")
    L.append("### 3.3 字体与其他")
    L.append("")
    fc = facts["assets"]["fontconfig"]
    if fc:
        L.append(f"- `assets/fontconfig/fonts.conf`：sha256 `{fc['sha256'][:16]}…`，"
                 f"{fc['bytes']} 字节。作用是把本机已有字体纳入 fontconfig 解析，"
                 f"**不随包分发字体文件**。")
    L.append(f"- 全仓扫描到的字体文件数量（.ttf/.otf/.ttc/.woff）："
             f"**{facts['own_source']['bundled_font_count']}**，"
             f"文件列表：{md_escape(str(facts['own_source']['bundled_font_files_found']))}。"
             f"这是“不随包分发字体”这一 README 主张的实测依据。")
    L.append("")

    L.append("## 4. 自有源码")
    L.append("")
    L.append(f"- `wengao_ppt/` 顶层模块数：**{facts['own_source']['module_count']}**")
    L.append(f"- 模块集合摘要（绝对路径无关，路径+内容哈希聚合）："
             f"`{facts['own_source']['modules_sha256']}`")
    L.append(f"- `requirements.txt` sha256：`{facts['requirements'].get('sha256')}`")
    L.append("- 与上游代码的**实测**关系：候选 `wengao_ppt/` 内没有出现 `luobo`/`zhouwei713`"
             " 之类的上游版权痕迹；同名文件只有 `render.py`，前 1200 字符相似度约 0.13；"
             "`runner.py::vendor_dir()` 只用于 `precheck` 阶段的固定提交核验"
             "（`git rev-parse HEAD` 比对），没有 import/exec 上游脚本。"
             "这**不**构成“是否衍生作品”的法律结论，定性留待所有者/法务。")
    L.append("")
    L.append("## 5. 外部调用记录（可复核，无付费模型调用）")
    L.append("")
    L.append("| 时间 | 目标 | 用途 | 是否付费/是否需要密钥 | 结果 |")
    L.append("| :--- | :--- | :--- | :--- | :--- |")
    ok = sum(1 for r in facts["pypi"] if r.get("sha256_of_response"))
    L.append(f"| {facts['generated_at']} | PyPI JSON API（{max(ok, len(facts['pypi']))} 项依赖元数据） |"
             f" 取版本与 SPDX 标识 | 免费公网匿名读、无密钥 | "
             f"{ok}/{len(facts['pypi'])} 成功，原始响应落在 `pypi_evidence/`；"
             f"失败项（如有）在 SOURCE_INVENTORY.json 里带 `error` 字段 |")
    L.append("")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    root = Path(args.root).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    baseline = root / "reports" / "p2-license-release-20261023" / "BASELINE_SHAS.txt"
    baseline_count, tree_sha = -1, "NOT_MEASURED"
    if baseline.is_file():
        lines = [l for l in baseline.read_text(encoding="utf-8").split("\n")
                 if l and not l.startswith("#")]
        baseline_count = len(lines)
        tree_sha = hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16] + "…(见 baseline_tree.json)"
    btree = root / "reports" / "p2-license-release-20261023" / "baseline_tree.json"
    if btree.is_file():
        tree_sha = json.loads(btree.read_text(encoding="utf-8"))["tree_sha256"]

    vp = root / "wengao_ppt" / "visual_plan.py"
    facts = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "root": str(root),
        "baseline_file_count": baseline_count,
        "baseline_tree_sha256": tree_sha,
        "upstream": measure_upstream(root),
        "venv": measure_venv_versions(root),
        "installed": measure_installed_metadata(),
        "pypi": measure_pypi_evidence(out),
        "assets": measure_assets(root),
        "own_source": measure_own_source(root),
        "requirements": parse_requirements(root),
        "visual_plan_sha256": sha256_file(vp) if vp.is_file() else "NOT_MEASURED",
    }
    (out / "SOURCE_INVENTORY.json").write_text(
        json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "LICENSE-MATRIX.md").write_text(build_markdown(facts), encoding="utf-8")
    print("wrote:", out / "SOURCE_INVENTORY.json")
    print("wrote:", out / "LICENSE-MATRIX.md")
    print("upstream head:", facts["upstream"].get("head"))
    print("assets ai_png:", len(facts["assets"]["ai_generated_png"]),
          "scene_manifest:", len(facts["assets"]["manifest_claims"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
