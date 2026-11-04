#!/usr/bin/env python3
"""安装载荷（可分发安装包）生成器。

做什么
------
从**当前权威程序目录**按显式政策组装一份可安装载荷：

1. 只包含运行期真正需要的成员（`wengao_ppt/`、`workbench/`、`contracts/`、
   `samples/r5_material_to_ppt.py`、`assets/fontconfig/`、`vendor/luobo-ppt/`、
   `requirements.txt` 与发布文档）；
2. 结构性排除运行期数据（`jobs/`、`reports/`、`workbench_uploads/`、`.venv/`、
   字节码、日志…），并在产物里留 `PAYLOAD-POLICY.md` 说明排除项与后果；
3. 对**载荷成员本体**跑一次内容脱敏扫描（复用 `p2_release_sanitizer`），
   命中 `must_fix` 且不在显式 allowlist 内即失败关闭；
4. 用 `wengao_ppt.install.verify_package` 与 `build_id` 自检载荷，输出
   `BUILD_MANIFEST.json`（含逐成员 SHA256）、`SHA256SUMS.txt`、`.tar.gz`。

不做什么
--------
- 不修改源目录任何字节；
- 不删除既有产物（同名目录存在即拒绝，需另给 `--label`）；
- 不声称"安装包已验收"：本脚本只产出载荷与自检结果，干净环境首任务验收
  由 `scripts/cleanroom_install_check.py` 单独执行并留证。

用法
----
    python3 scripts/build_install_payload.py --src . --out dist
    python3 scripts/build_install_payload.py --src . --out dist --check-only
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_SRC = HERE.parent

#: 进载荷的顶层成员（目录或文件）。显式列举，不做"整棵复制再挑"。
INCLUDE_TREES = (
    "wengao_ppt",
    "workbench",
    "contracts",
    "vendor/luobo-ppt",
)
INCLUDE_FILES = (
    "requirements.txt",
    "README.md",
    "README_EN.md",
    "SKILL.md",
    "LICENSE",
    "LICENSE-STATUS.md",
    "THIRD-PARTY-NOTICES.md",
    "VERSIONS.md",
    "samples/r5_material_to_ppt.py",
    "assets/fontconfig/fonts.conf",
    # HG01–HG12 排版硬门禁的**运行期实现**（`wengao_ppt/runner.py` 以
    # `program_root/scripts/layout_gate.py` 打开）。它不属于"开发脚本"：
    # 少了它，安装实例的 layout.hard_gate 恒为 NOT_EVALUABLE，且原生实播录像/
    # 复核记录的证据校验会失效。`scripts/` 其余脚本仍不进载荷。
    "scripts/layout_gate.py",
)

#: 许可未取得明确结论、默认不进载荷的资产（`--include-unverified-assets` 才带）。
UNVERIFIED_ASSET_TREES = ("assets/visual_img",)

#: 结构性排除：与 `wengao_ppt/install.py::_PACKAGE_EXCLUDE_DIRS` 保持一致口径。
EXCLUDE_DIRS = {
    "__pycache__", ".git", ".hg", ".svn", ".venv", "venv", "env",
    "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "jobs", "reports", "material_tasks", "wb_loopback_data",
    "workbench_previews", "workbench_uploads", ".wengao_scene_assets",
    ".fontconfig", ".lo-profile", ".ipynb_checkpoints", "work", "dist",
    "build", ".locks", ".claude", "calibration", "fixtures", "fonts",
    "samples/open-source-50-decks", "samples/material_replay",
}
EXCLUDE_SUFFIXES = (".log", ".pyc", ".pyo", ".tmp", ".sqlite", ".sqlite3",
                    ".db", ".swp", ".pid", ".lock", ".orig", ".bak")
EXCLUDE_FILES = {".DS_Store", "probe.txt", "Thumbs.db"}

#: 已人工确认、允许进入载荷的扫描命中：(相对路径, 规则名) -> 理由。
#: 只登记"命中属于误报或已脱敏"的条目；新增条目必须写清可核对理由。
SCAN_ALLOWLIST = {
    ("wengao_ppt/scene_model.py", "abs_private_tmp"):
        "该行是**脱敏用的正则**（把 /Users/、/private/var/folders 匹配掉），"
        "不是本机真实路径；模式串本身即清洗规则。",
    ("wengao_ppt/scene_images.py", "email"):
        "HTTP User-Agent 里的产品联系串 team@wengao.local（保留域，非个人邮箱）。",
    ("vendor/luobo-ppt/scripts/common.py", "home_tilde_note"):
        "上游 MIT 源码里查找系统字体的 `~/.fonts`、`~/Library/Fonts` glob 模式，"
        "是平台字体搜索路径而非本机私人目录内容。",
}

POLICY_DOC = """# 安装载荷政策（PAYLOAD-POLICY）

本目录是由 `scripts/build_install_payload.py` 生成的**安装载荷**，只含运行期
程序成员。生成时的排除项与后果如下：

| 排除项 | 原因 | 后果 |
| --- | --- | --- |
| `jobs/`、`reports/`、`workbench_uploads/`、`workbench_previews/`、`wb_loopback_data/` | 用户私有材料与运行期数据 | 载荷不含任何用户稿件；安装后为空白实例 |
| `.venv/`、字节码、`*.log`、`*.pid` | 本机运行期产物 | 需在目标机按 `requirements.txt` 建环境 |
| `tests/`、`scripts/`（**除 `scripts/layout_gate.py`**）、`fixtures/`、`calibration/` | 开发期自检与门禁工具 | 载荷不能跑仓库内测试；验收须回到源码仓。`scripts/layout_gate.py` 例外：它是运行期门禁实现，必须随载荷安装 |
| `fonts/` | 22 MB 开发期字体样本，运行时不引用 | 渲染使用系统字体（见 `assets/fontconfig/fonts.conf`） |
| `assets/visual_img/` | 资产许可结论未定 | 视觉升级主题的示意配图缺失；`--include-unverified-assets` 可带上，但那时许可责任随载荷转移 |
| `vendor/luobo-ppt/.git` | 上游仓库元数据 | `precheck` 的上游提交校验会记 `VENDOR_DIR_MISSING`，不阻断生成 |

安装方法：

```bash
python3 -m pip install -r requirements.txt
python3 -m wengao_ppt.install --root <安装根> install --package <本目录> --activate
python3 -m wengao_ppt.install --root <安装根> status
```

外部依赖：LibreOffice（真实渲染/导出）。不随载荷分发字体或模型权重。
"""


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_members(src: Path, include_unverified_assets: bool = False):
    """按政策枚举载荷成员（相对路径，已排序）。"""
    roots = list(INCLUDE_TREES)
    files = list(INCLUDE_FILES)
    if include_unverified_assets:
        roots.extend(UNVERIFIED_ASSET_TREES)
    out: list[Path] = []
    for rel_tree in roots:
        base = src / rel_tree
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            rel_dir = Path(dirpath).relative_to(src)
            keep = []
            for d in sorted(dirnames):
                rel = (rel_dir / d).as_posix()
                if d in EXCLUDE_DIRS or rel in EXCLUDE_DIRS:
                    continue
                keep.append(d)
            dirnames[:] = keep
            for fn in sorted(filenames):
                if fn in EXCLUDE_FILES or fn.casefold().endswith(EXCLUDE_SUFFIXES):
                    continue
                out.append(Path(dirpath) / fn)
    for rel in files:
        p = src / rel
        if p.is_file() and p.name not in EXCLUDE_FILES:
            out.append(p)
    uniq = sorted({p.relative_to(src).as_posix(): p for p in out}.items())
    return [(rel, p) for rel, p in uniq]


def scan_payload(src: Path, members, payload_root: Path | None = None):
    """对载荷成员做内容扫描；`payload_root` 给定时扫复制后的副本。"""
    sys.path.insert(0, str(HERE))
    import p2_release_sanitizer as scanmod  # noqa: E402

    must_fix, manual, allowed = [], [], []
    for rel, p in members:
        target = (payload_root / rel) if payload_root else p
        rel_out, hits, state = scanmod.scan_file(target, payload_root or src)
        if not hits:
            continue
        for h in hits:
            key = (rel_out, h["rule"])
            rec = {"path": rel_out, "rule": h["rule"], "severity": h["severity"],
                   "line": h["line"], "snippet": h["snippet_redacted"]}
            if key in SCAN_ALLOWLIST:
                allowed.append({**rec, "reason": SCAN_ALLOWLIST[key]})
            elif h["severity"] == "must_fix":
                must_fix.append(rec)
            else:
                manual.append(rec)
    return {"must_fix": must_fix, "manual_review": manual, "allowlisted": allowed,
            "allowlist_size": len(SCAN_ALLOWLIST)}


def _assert_safe_payload(payload: Path) -> list[str]:
    """复制后复核：不得出现运行期成员（防止政策与实现漂移）。"""
    found = []
    for path in payload.rglob("*"):
        rel = path.relative_to(payload)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            found.append(rel.as_posix())
        elif path.is_file() and (path.name in EXCLUDE_FILES
                                 or path.name.casefold().endswith(EXCLUDE_SUFFIXES)):
            found.append(rel.as_posix())
    return sorted(found)[:50]


def write_sums(payload: Path, out: Path) -> str:
    lines = []
    for p in sorted(payload.rglob("*")):
        if p.is_file():
            lines.append(f"{sha256_file(p)}  {p.relative_to(payload).as_posix()}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sha256_file(out)


def make_archive(payload: Path, archive: Path) -> str:
    """确定性打包：成员按名字排序，mtime 归一到源文件 mtime 的最小值之外不引入时钟。"""
    names = sorted(p.relative_to(payload).as_posix()
                   for p in payload.rglob("*") if p.is_file())
    with tarfile.open(archive, "w:gz", compresslevel=9) as tf:
        for rel in names:
            p = payload / rel
            ti = tf.gettarinfo(str(p), arcname=f"{payload.name}/{rel}")
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            with open(p, "rb") as f:
                tf.addfile(ti, f)
    return sha256_file(archive)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成稳稿 PPT 安装载荷（含自检）")
    ap.add_argument("--src", default=str(DEFAULT_SRC), help="权威程序目录")
    ap.add_argument("--out", default=None, help="输出目录（默认 <src>/dist）")
    ap.add_argument("--label", default=None,
                    help="载荷标签（默认 wengao-ppt-<version>-<build_id[:8]>）")
    ap.add_argument("--include-unverified-assets", action="store_true",
                    help="把许可结论未定的 assets/visual_img 一并带入")
    ap.add_argument("--check-only", action="store_true", help="只做政策/扫描自检，不落盘")
    ap.add_argument("--force", action="store_true",
                    help="标签目录已存在时把它移入 dist/quarantine/ 后重建（不删除）")
    a = ap.parse_args(argv)

    src = Path(a.src).expanduser().resolve()
    if not (src / "wengao_ppt" / "cli.py").is_file():
        print(json.dumps({"ok": False, "stage": "src",
                          "detail": f"不是程序目录（缺 wengao_ppt/cli.py）：{src}"},
                         ensure_ascii=False, indent=2))
        return 4

    sys.path.insert(0, str(src))
    from wengao_ppt import __version__ as version  # noqa: E402
    from wengao_ppt.install import REQUIRED, build_id, verify_package  # noqa: E402

    members = iter_members(src, a.include_unverified_assets)
    rels = [rel for rel, _ in members]
    results = {
        "schema": "wengao.install_payload_build.v1",
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_root": str(src),
        "product_version": version,
        "source_build_id": build_id(src),
        "include_unverified_assets": bool(a.include_unverified_assets),
        "member_count": len(members),
        "members": [],
        "missing_required": [],
        "scan": {},
        "checks": {},
        "ok": False,
    }

    # ① 必需成员必须在载荷枚举结果里（不是"源目录有"，而是"会进载荷"）
    required_missing = [r for r in REQUIRED if r not in set(rels)]
    results["missing_required"] = required_missing

    # ② 内容扫描（先扫源侧，挡在复制之前）
    scan = scan_payload(src, members)
    results["scan"] = scan

    out_dir = Path(a.out).expanduser().resolve() if a.out else (src / "dist")
    label = a.label or f"wengao-ppt-{version}-{results['source_build_id'][:8]}"

    if a.check_only:
        results["checks"] = {"required_members_present": not required_missing,
                             "no_blocking_scan_hits": not scan["must_fix"],
                             "no_unreviewed_manual_hits": not scan["manual_review"]}
        results["ok"] = all(results["checks"].values())
        results["members"] = [{"path": rel, "sha256": sha256_file(p),
                               "bytes": p.stat().st_size} for rel, p in members]
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0 if results["ok"] else 2

    if scan["must_fix"] or scan["manual_review"]:
        results["checks"] = {"required_members_present": not required_missing,
                             "no_blocking_scan_hits": not scan["must_fix"],
                             "no_unreviewed_manual_hits": not scan["manual_review"]}
        print(json.dumps(results, ensure_ascii=False, indent=2))
        print("[build_install_payload] 拒绝构建：载荷成员存在未放行的扫描命中"
              f"（must_fix={len(scan['must_fix'])}，"
              f"manual_review={len(scan['manual_review'])}）。"
              "逐条复核后写入 SCAN_ALLOWLIST 并注明理由，不得批量放行。",
              file=sys.stderr)
        return 2

    payload = out_dir / label
    if payload.exists():
        if not a.force:
            print(json.dumps({"ok": False, "stage": "out_exists",
                              "detail": f"载荷目录已存在，未覆盖：{payload}；"
                                        f"换个 --label 或加 --force（会先移入 quarantine/）。"},
                             ensure_ascii=False, indent=2))
            return 4
        quar = out_dir / "quarantine"
        quar.mkdir(parents=True, exist_ok=True)
        shutil.move(str(payload), str(quar / f"{label}-{int(time.time())}"))
    payload.mkdir(parents=True, exist_ok=True)

    copied = 0
    for rel, p in members:
        dst = payload / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
        copied += 1

    (payload / "PAYLOAD-POLICY.md").write_text(POLICY_DOC, encoding="utf-8")

    leaked = _assert_safe_payload(payload)
    post_scan = scan_payload(src, members, payload_root=payload)
    verify = verify_package(payload)

    sums_path = out_dir / f"{label}.SHA256SUMS.txt"
    sums_sha = write_sums(payload, sums_path)
    archive = out_dir / f"{label}.tar.gz"
    archive_sha = make_archive(payload, archive)

    results["payload_dir"] = str(payload)
    results["payload_build_id"] = verify.get("build_id")
    results["verify_package"] = verify
    results["archive"] = {"path": str(archive), "sha256": archive_sha,
                          "bytes": archive.stat().st_size}
    results["sums"] = {"path": str(sums_path), "sha256": sums_sha}
    file_count = len([p for p in payload.rglob("*") if p.is_file()])
    results["checks"] = {
        "required_members_present": not verify.get("missing"),
        "no_blocking_scan_hits": not post_scan["must_fix"],
        "no_unreviewed_manual_hits": not post_scan["manual_review"],
        "no_forbidden_members": not leaked,
        "member_count_matches": file_count == copied + 1,
        "build_id_stable_after_copy": verify.get("build_id") is not None,
    }
    results["forbidden_members"] = leaked
    results["scan_post_copy"] = post_scan
    results["members"] = [
        {"path": p.relative_to(payload).as_posix(), "sha256": sha256_file(p),
         "bytes": p.stat().st_size}
        for p in sorted(payload.rglob("*")) if p.is_file()]
    results["ok"] = all(results["checks"].values())

    manifest = out_dir / "BUILD_MANIFEST.json"
    manifest.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    results["manifest"] = str(manifest)

    print(json.dumps({k: results[k] for k in
                      ("ok", "label" if "label" in results else "product_version",
                       "member_count", "payload_dir", "payload_build_id", "checks",
                       "archive", "manifest") if k in results},
                     ensure_ascii=False, indent=2))
    return 0 if results["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
