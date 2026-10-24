#!/usr/bin/env python3
"""scripts/rs8_extend_identity_contract.py —— RS-8 身份契约扩展补丁（预置，未到触发条件不得执行）。

仅当 RS-2/3/4 完成且 RS-7 三档通过后，由 W4 owner 执行：
    python scripts/rs8_extend_identity_contract.py --apply

默认 dry-run：只打印将发生的变更。fail-closed：任何一处与期望基线不一致即整体拒绝。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = ROOT / "contracts" / "prepublication_50_decks_identity_v1.json"

EXPECTED_PROVIDER_ENUM = "openverse | wikimedia_commons | pexels"
NEW_PROVIDER_ENUM = "openverse | wikimedia_commons | pexels | ai_generated_local"
EXPECTED_LICENSE_ENUM = "CC0 | Public Domain Mark | Public Domain | CC BY 4.0 | Pexels License"
# AI 生成许可取值以 W1 登记的 ComfyUI 模型许可为准；占位串在 apply 前由 owner 核对
NEW_LICENSE_ENUM = EXPECTED_LICENSE_ENUM + " | Local Model License (per generator registration)"

NEW_DECK_FIELDS = {
    "independent_content_fidelity_verdict": "string，必须为 'PASS'（P8 独有项并入）",
    "layout_and_placement_safety_verdict": "string，必须为 'PASS'（P8 独有项并入）",
    "visual_review_verdict": "string，必须为 'PASS'（P8 独有项并入）",
    "research_evidence": "对象：bundle/queries/snapshots 的相对路径与 sha256；缺研究证据且 research.enabled=true 即拒",
}

NEW_IMAGE_FIELDS = {
    "generator": "AI 生成时的生成器名（comfyui）；非 AI 生成可为 null",
    "prompt_digest": "AI 生成提示词摘要的 sha256；非 AI 生成可为 null",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写盘；默认 dry-run")
    args = ap.parse_args()

    if not IDENTITY.is_file():
        print(f"找不到 {IDENTITY}", file=sys.stderr)
        return 2
    text = IDENTITY.read_text(encoding="utf-8")
    doc = json.loads(text)

    problems = []
    if EXPECTED_PROVIDER_ENUM not in text:
        problems.append("image_provider 枚举与期望基线不一致（可能已扩展或文件被改）")
    if EXPECTED_LICENSE_ENUM not in text:
        problems.append("image_license 枚举与期望基线不一致")
    if problems:
        print("fail-closed：拒绝修改")
        for p in problems:
            print("  - " + p)
        return 1

    changes = []
    if NEW_PROVIDER_ENUM not in text:
        changes.append(f"image_provider: {EXPECTED_PROVIDER_ENUM} -> {NEW_PROVIDER_ENUM}")
    if NEW_LICENSE_ENUM not in text:
        changes.append(f"image_license: 追加 AI 生成许可取值")
    deck_fields = doc.get("deck_entry_required_fields") or {}
    for k, v in NEW_DECK_FIELDS.items():
        if k not in deck_fields:
            changes.append(f"deck_entry_required_fields += {k}")
    img_fields = doc.get("image_ledger_entry_fields") or {}
    for k, v in NEW_IMAGE_FIELDS.items():
        if k not in img_fields and "image_ledger" not in str(img_fields)[:0]:
            changes.append(f"image ledger 字段 += {k}（generator/prompt_digest）")

    print("RS-8 身份契约扩展（dry-run）：")
    for c in changes:
        print("  * " + c)
    if not changes:
        print("  （无需变更）")
        return 0
    if not args.apply:
        print("\n未加 --apply，不写盘。触发条件：RS-2/3/4 完成且 RS-7 三档通过。")
        return 0

    text = text.replace(EXPECTED_PROVIDER_ENUM, NEW_PROVIDER_ENUM)
    text = text.replace(EXPECTED_LICENSE_ENUM, NEW_LICENSE_ENUM)
    doc = json.loads(text)
    doc.setdefault("deck_entry_required_fields", {}).update(NEW_DECK_FIELDS)
    IDENTITY.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("已写盘。别忘了同步更新 tests/test_prepublication_50_decks_gate_negatives.py 负例。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
