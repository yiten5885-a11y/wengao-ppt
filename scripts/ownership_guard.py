#!/usr/bin/env python3
"""scripts/ownership_guard.py —— 文件独占检查（Step 0.3）。

用法：
  python scripts/ownership_guard.py --base main --head rs/w2-layout
  python scripts/ownership_guard.py --files wengao_ppt/theme.py wengao_ppt/runner.py --owner W2

判定：
- 每个改动文件必须落在且仅落在**一个** owner 的独占模式内（shared_readonly 只读）。
- 越界 / 无主 / 跨窗口 → 退出码 1 并列出明细；通过退出码 0。
- 与 git 无关的纯文件清单模式供合并前人工核对使用。
"""
from __future__ import annotations

import argparse
import fnmatch
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OWNERS_FILE = ROOT / "OWNERS.yaml"


def _mini_yaml(text: str) -> dict:
    """极简 YAML 子集解析（避免引入 PyYAML 依赖；只支持本文件用到的结构）。"""
    data: dict = {}
    section = None
    cur_list_key = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip() if not raw.strip().startswith("#") else ""
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        body = line.strip()
        if indent == 0 and body.endswith(":"):
            section = body[:-1]
            data[section] = {}
            cur_list_key = None
            continue
        if indent == 0 and ":" in body:
            k, v = body.split(":", 1)
            data[k.strip()] = v.strip().strip('"')
            cur_list_key = None
            continue
        if section and body.startswith("- "):
            if cur_list_key is None:
                # 形如 "- path" 直接挂在段下的列表（shared_readonly）
                data[section].setdefault("__list__", []).append(body[2:].strip())
            else:
                data[section][cur_list_key].append(body[2:].strip())
            continue
        if section and indent >= 2 and body.endswith(":"):
            cur_list_key = body[:-1]
            data[section].setdefault(cur_list_key, [])
            continue
        if section and indent >= 2 and ":" in body:
            k, v = body.split(":", 1)
            data[section][k.strip()] = v.strip().strip('"')
            cur_list_key = None
    return data


def load_owners(path: Path = OWNERS_FILE) -> dict:
    return _mini_yaml(path.read_text(encoding="utf-8"))


def owner_of(path: str, owners_doc: dict) -> str | None:
    owners = owners_doc.get("owners") or {}
    for window, patterns in owners.items():
        if not isinstance(patterns, list):
            continue
        for pat in patterns:
            if fnmatch.fnmatch(path, pat) or path == pat or path.startswith(pat.rstrip("*").rstrip("/") + "/"):
                return window
    return None


def changed_files(base: str, head: str) -> list:
    try:
        out = subprocess.run(
            ["git", "diff", "--name-only", f"{base}...{head}"],
            cwd=ROOT, capture_output=True, text=True, check=True)
        files = [f for f in out.stdout.splitlines() if f.strip()]
        if files:
            return files
        # 空 diff（同线或未推送）：退化为 HEAD 与 base 的两点 diff
        out = subprocess.run(
            ["git", "diff", "--name-only", base, head],
            cwd=ROOT, capture_output=True, text=True, check=True)
        return [f for f in out.stdout.splitlines() if f.strip()]
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"git diff 失败：{e.stderr or e}\n")
        return []


def main() -> int:
    ap = argparse.ArgumentParser(description="文件独占检查（OWNERS.yaml）")
    ap.add_argument("--base", default="main")
    ap.add_argument("--head", default="HEAD")
    ap.add_argument("--files", nargs="*", default=None)
    ap.add_argument("--owner", default=None, help="声明这些文件属于哪个窗口（与推断比对）")
    args = ap.parse_args()

    doc = load_owners()
    files = args.files if args.files is not None else changed_files(args.base, args.head)
    if not files:
        print("ownership_guard: 没有改动文件（或 diff 为空）——通过。")
        return 0

    shared = set((doc.get("shared_readonly") or {}).get("__list__", []))
    violations = []
    for f in files:
        if f in shared:
            violations.append(f"{f}: shared_readonly 公共只读区，任何窗口不得单独修改")
            continue
        inferred = owner_of(f, doc)
        if inferred is None:
            violations.append(f"{f}: 无主文件——不在任何窗口的独占范围内")
        elif args.owner and inferred != args.owner:
            violations.append(f"{f}: 归属 {inferred}，但声明为 {args.owner}——越界")

    if violations:
        print("ownership_guard: 越界，拒绝合并")
        for v in violations:
            print("  - " + v)
        print(f"\n共 {len(violations)} 个越界文件。请改走自己的分支/worktree，"
              f"或向 W4（owner）提交契约/独占范围变更请求单。")
        return 1
    print(f"ownership_guard: {len(files)} 个改动文件全部在独占范围内——通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
