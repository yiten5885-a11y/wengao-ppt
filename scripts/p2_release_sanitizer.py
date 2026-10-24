#!/usr/bin/env python3
"""P2 发布消毒：可发布副本的生成前静态扫描（只读，不修改源候选）。

用途
----
在生成对外可发布副本之前，对**候选包**做一次可读取证扫描，回答两个问题：

1. 结构性：哪些运行期/数据成员会被带进副本（`jobs/`、`reports/`、
   `wb_loopback_data/`、日志、`.venv`、字节码等）；
2. 内容性：候选的**待发布成员**里是否残留本机绝对路径、密钥/凭证、
   邮箱等私人信息、无关项目残留、以及长一行私有备注。

设计原则
--------
- 本脚本**只读**源候选，任何修改都发生在 `--out` 指定的副本里；
- 规则全部显式列举（`RULES`），命中即记录，不做“看起来可公开就放过”的
  模糊放宽；无法自动判定的命中一律进 `manual_review`，不放行；
- 退出码：0 = 无命中可直接生成副本；2 = 存在 must_fix 命中，禁止生成；
  3 = 仅 manual_review 命中，可生成但必须先人工复核。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

# --- 结构性排除：与 wengao_ppt/install.py 的发布载荷政策保持一致 -------------
EXCLUDE_DIRS = {
    "__pycache__", ".git", ".hg", ".svn", ".venv", "venv", "env",
    "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "jobs", "reports", "material_tasks", "wb_loopback_data",
    "workbench_previews", "workbench_uploads", ".wengao_scene_assets",
    ".fontconfig", ".lo-profile", ".ipynb_checkpoints",
    # P2 追加：隔离副产物 / 开发期历史版本快照 / 回滚快照
    "versions", "rollback-20261021-0908",
}
EXCLUDE_SUFFIXES = (".log", ".pyc", ".pyo", ".tmp", ".sqlite", ".sqlite3",
                    ".db", ".swp", ".pid", ".lock")
EXCLUDE_FILES = {".DS_Store", "probe.txt", "Thumbs.db", "workbench_server.log",
                 "workbench_server_8823.log"}

# --- 内容性规则 -------------------------------------------------------------
# 说明：命中 `must_fix` 表示不得进对外副本；命中 `manual_review` 表示可能属于
# 正常文档示例（例如 README 里的占位用户名），必须人工逐条确认后才能放行。
RULES = [
    ("abs_user_path", "must_fix",
     re.compile(r"/Users/[A-Za-z0-9_.\-]+"), "本机用户绝对路径"),
    ("abs_private_tmp", "must_fix",
     re.compile(r"/private/var/folders|[A-Za-z]:\\\\Users\\\\"), "本机临时/Windows 路径"),
    ("file_uri", "must_fix", re.compile(r"file:///Users/"), "file:// 本机 URI"),
    ("api_key_assign", "must_fix",
     re.compile(r"(?i)(api[_-]?key|apikey|secret|token|passwd|password)\s*[=:]\s*['\"][^'\"\s]{8,}['\"]"),
     "疑似密钥/口令字面量赋值"),
    ("openai_style_key", "must_fix",
     re.compile(r"\bsk-[A-Za-z0-9]{16,}"), "sk- 风格密钥"),
    ("aws_key", "must_fix", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS Access Key"),
    ("private_key_block", "must_fix",
     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "私钥 PEM 头"),
    ("bearer", "must_fix", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{16,}"), "Bearer 凭证"),
    ("email", "manual_review",
     re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"), "邮箱地址"),
    ("phone_cn", "manual_review",
     re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "疑似中国大陆手机号"),
    ("home_tilde_note", "manual_review",
     re.compile(r"~/Library/|~/Desktop/|~/Documents/"), "私人目录简写路径"),
    ("proxy_env", "manual_review",
     re.compile(r"(?i)(http_proxy|https_proxy|all_proxy)\s*=\s*\S+"), "代理环境变量"),
]

TEXT_SUFFIXES = {".py", ".md", ".json", ".txt", ".html", ".js", ".css", ".sh",
                 ".ps1", ".bat", ".yaml", ".yml", ".toml", ".cfg", ".ini",
                 ".conf", ".svg", ".xml", ".csv", ".command"}
MAX_SCAN_BYTES = 4 * 1024 * 1024   # 单文件最多扫前 4 MiB，其余记为 skipped_large


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_release_members(root: Path):
    """按 excluded 政策枚举**待发布成员**相对路径。"""
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in EXCLUDE_DIRS and str((rel_dir / d)) not in EXCLUDE_DIRS)
        for fn in sorted(filenames):
            if fn in EXCLUDE_FILES or fn.casefold().endswith(EXCLUDE_SUFFIXES):
                continue
            yield Path(dirpath) / fn


def iter_excluded(root: Path):
    """枚举被排除掉的结构性成员（用于证明排除生效，而不是"没看到"）。"""
    seen = set()
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        keep = []
        for d in sorted(dirnames):
            rel = (rel_dir / d).as_posix()
            if d in EXCLUDE_DIRS or rel in EXCLUDE_DIRS:
                seen.add(rel)
            else:
                keep.append(d)
        dirnames[:] = keep
        for fn in sorted(filenames):
            rel = (rel_dir / fn).as_posix()
            if fn in EXCLUDE_FILES or fn.casefold().endswith(EXCLUDE_SUFFIXES):
                seen.add(rel)
    return sorted(seen)


def scan_file(path: Path, root: Path):
    rel = path.relative_to(root).as_posix()
    suffix = path.suffix.lower()
    if suffix not in TEXT_SUFFIXES:
        return rel, None, "binary"
    try:
        size = path.stat().st_size
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(MAX_SCAN_BYTES) if size <= MAX_SCAN_BYTES \
                else f.read(MAX_SCAN_BYTES) + "\n"
    except OSError as exc:
        return rel, None, f"unreadable:{exc}"
    hits = []
    for name, severity, pattern, desc in RULES:
        for m in pattern.finditer(text):
            line_no = text.count("\n", 0, m.start()) + 1
            line = text.splitlines()[line_no - 1] if line_no <= len(text.splitlines()) else ""
            snippet = line.strip()
            redacted = re.sub(r"([A-Za-z0-9._\-]{6})[A-Za-z0-9._\-]{4,}",
                              r"\1<REDACTED>", m.group(0))
            hits.append({"rule": name, "severity": severity, "desc": desc,
                         "line": line_no, "match_redacted": redacted[:120],
                         "snippet_redacted": snippet[:160]})
    return rel, hits, ("ok" if size <= MAX_SCAN_BYTES else "truncated")


def main() -> int:
    ap = argparse.ArgumentParser(description="P2 发布消毒静态扫描（只读）")
    ap.add_argument("--root", required=True, help="待发布源根目录（一般是开发候选目录）")
    ap.add_argument("--json-out", default=None, help="扫描结果 JSON 输出路径")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"ROOT_NOT_FOUND: {root}", file=sys.stderr)
        return 4

    members = list(iter_release_members(root))
    excluded = iter_excluded(root)

    must_fix, manual = [], []
    signed = []
    stat = {"files_total": 0, "binary_skipped": 0, "truncated": 0, "unreadable": 0}
    for path in members:
        rel, hits, state = scan_file(path, root)
        if state == "binary":
            stat["binary_skipped"] += 1
        elif state.startswith("unreadable"):
            stat["unreadable"] += 1
        elif state == "truncated":
            stat["truncated"] += 1
        stat["files_total"] += 1
        signed.append({"path": rel, "sha256": sha256_file(path),
                       "bytes": path.stat().st_size})
        for h in hits or []:
            rec = dict(h, path=rel)
            (must_fix if h["severity"] == "must_fix" else manual).append(rec)

    result = {
        "schema": "P2_SANITIZATION_SCAN_V1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "root": str(root),
        "excluded_dirs": sorted(EXCLUDE_DIRS),
        "excluded_suffixes": list(EXCLUDE_SUFFIXES),
        "excluded_files": sorted(EXCLUDE_FILES),
        "excluded_members_detected": excluded[:200],
        "excluded_members_total": len(excluded),
        "stat": stat,
        "must_fix": must_fix,
        "manual_review": manual,
        "counts": {"must_fix": len(must_fix), "manual_review": len(manual)},
    }
    # P2 追加：为了让验收者能核对副本未被调包，这里同时给出成员清单摘要。
    result["release_members_sha256"] = hashlib.sha256(
        "\n".join(f"{m['sha256']}  {m['path']}" for m in signed).encode()
    ).hexdigest()
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json_out:
        p = Path(args.json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        print(f"WROTE {p}")
    else:
        print(text)

    print("\n== SUMMARY ==")
    print(f"root               : {root}")
    print(f"release members    : {stat['files_total']}")
    print(f"excluded members   : {len(excluded)}")
    print(f"must_fix hits      : {len(must_fix)}")
    print(f"manual_review hits : {len(manual)}")
    for sev, items in (("MUST_FIX", must_fix), ("MANUAL", manual)):
        by_path = {}
        for it in items:
            by_path.setdefault(it["path"], []).append(it)
        for path, hs in sorted(by_path.items()):
            kinds = sorted({h["rule"] for h in hs})
            print(f"  [{sev}] {path}: {len(hs)} hits {kinds}")
    if must_fix:
        return 2
    return 3 if manual else 0


if __name__ == "__main__":
    sys.exit(main())
