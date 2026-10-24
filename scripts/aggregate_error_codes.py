#!/usr/bin/env python3
"""scripts/aggregate_error_codes.py —— 错误码汇总（Step 0.5）。

各窗口只写自己命名空间的 `contracts/errors/<ns>.json`；本脚本把它们汇总成
一张总表（stdout / 可选 --out 落盘），供 `error_codes_v1.json` 权威字典对照：
  - 命名空间重复定义 → 报错退出（不允许多人改同一个文件）
  - 同一 code 出现在两个命名空间 → 报错退出
  - 输出按命名空间排序的 JSON 汇总

用法：
  python scripts/aggregate_error_codes.py [--out reports/rs-w4/error_codes_summary.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ERR_DIR = ROOT / "contracts" / "errors"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    namespaces: dict[str, dict] = {}
    seen_codes: dict[str, str] = {}
    problems: list[str] = []
    for p in sorted(ERR_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except ValueError as e:
            problems.append(f"{p.name}: JSON 解析失败：{e}")
            continue
        ns = d.get("namespace")
        if not ns:
            problems.append(f"{p.name}: 缺少 namespace 字段")
            continue
        if ns in namespaces:
            problems.append(f"{p.name}: 命名空间 {ns} 重复定义（已由 {namespaces[ns]['file']} 持有）")
            continue
        codes = d.get("codes") or []
        for c in codes:
            code = c.get("code", "")
            if code in seen_codes:
                problems.append(f"{p.name}: 错误码 {code} 与 {seen_codes[code]} 重复")
            seen_codes[code] = f"{p.name}({ns})"
        namespaces[ns] = {"file": p.name, "owner": d.get("owner"), "codes": codes}

    summary = {
        "generated_by": "scripts/aggregate_error_codes.py",
        "namespaces": {k: {"file": v["file"], "owner": v["owner"],
                           "code_count": len(v["codes"])}
                       for k, v in sorted(namespaces.items())},
        "total_codes": len(seen_codes),
        "codes": dict(sorted(seen_codes.items())),
    }
    if args.out:
        out = ROOT / args.out if not Path(args.out).is_absolute() else Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"汇总已写入 {out}")

    if problems:
        print("错误码汇总发现问题：")
        for x in problems:
            print("  - " + x)
        return 1
    print(f"命名空间 {len(namespaces)} 个，错误码 {len(seen_codes)} 个，无重复。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
