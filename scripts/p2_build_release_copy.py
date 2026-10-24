#!/usr/bin/env python3
"""P2 可发布消毒副本生成器（默认只读预览，`--apply` 才落盘）。

做什么
------
把开发候选目录按**显式政策**复制到邻接的发布目录，并在同一进程里完成：

1. 结构性排除（`jobs/ reports/ wb_loopback_data/ .venv/ __pycache__/ .git/
   日志/临时文件`等）；
2. 逐文件、逐条收取额外排除清单（`EXTRA_EXCLUDE`），每条写明理由；
3. 内容级脱敏（`REDACTIONS`）：对必须进副本、但携带本机绝对路径的行做
   **确定性替换**，并记录替换前后 sha256，便于验收者重算比对；
4. 资产政策：默认排除尚未取得再分发许可的图片资产（`UNVERIFIED_ASSETS`），
   并在副本里留下 `ASSET-EXCLUSIONS.md` 说明缺失后果；
5. 复制完成后**对副本本体再跑一次同样的静态扫描**（复用
   `p2_release_sanitizer` 的规则），出现 `must_fix` 立即失败关闭。

不做什么
--------
- 不修改源候选的任何字节（源侧残留问题写入 `SOURCE-SIDE-FINDINGS.md`）；
- 不猜测任何资产的权属或许可结论；不确定的一律标为阻断项，交所有者确认；
- 不因为"副本扫描干净"就宣称源候选干净。

用法
----
    python3 scripts/p2_build_release_copy.py --src <候选目录> --out <副本目录> \\
        --report <报告目录> [--include-unverified-assets] [--apply]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p2_release_sanitizer as scanmod  # noqa: E402
import prepublication_50_decks_gate as deckgate  # noqa: E402

PLACEHOLDER = "<REDACTED_LOCAL_PATH>"

#: 逐文件额外排除：这些文件属于开发期一次性 QA 脚本 / 打包工具，
#: 各自携带本机绝对路径或仅供本仓自检使用，不属于用户可安装载荷。
#: 每条给出**为什么不进副本**的可核对理由。
EXTRA_EXCLUDE_FILES = {
    # 一次性渲染/字形检查脚本：把候选绝对路径写死在文件头
    "scripts/inspect_r3.py": "dev-only: 文件头写死本机候选绝对路径",
    "scripts/inspect_r3b.py": "dev-only: 文件头写死本机候选绝对路径",
    "scripts/inspect_r3c.py": "dev-only: 文件头写死本机候选绝对路径",
    "scripts/inspect_r3d.py": "dev-only: 文件头写死本机候选绝对路径",
    "scripts/inspect_r3e.py": "dev-only: 文件头写死本机候选绝对路径",
    "scripts/inspect_r3f.py": "dev-only: 文件头写死本机候选绝对路径",
    "scripts/inspect_r3g.py": "dev-only: 文件头写死本机候选绝对路径",
    "scripts/inspect_r3h.py": "dev-only: 文件头写死本机候选绝对路径",
    "scripts/inspect_r3i.py": "dev-only: 文件头写死本机候选绝对路径",
    "scripts/inspect_r3j.py": "dev-only: 文件头写死本机候选绝对路径",
    "scripts/inspect_r3j2.py": "dev-only: 文件头写死本机候选绝对路径",
    "scripts/inspect_r3k.py": "dev-only: 文件头写死本机候选绝对路径",
    "scripts/inspect_baseline_style.py": "dev-only: 一次性版式检查脚本",
    "scripts/run_gate_13p.py": "dev-only: 写死外部监督工作区绝对路径",
    "scripts/run_gate_negative_cases.py": "dev-only: 写死外部监督工作区绝对路径",
    "scripts/run_negative_regression.py": "dev-only: 写死外部监督工作区绝对路径",
    "scripts/p2_release_sanitizer.py": "dev-only: P2 打包审计工具，不属于用户运行载荷",
    "scripts/p2_build_release_copy.py": "dev-only: P2 打包审计工具，不属于用户运行载荷",
    "scripts/p2_build_license_matrix.py": "dev-only: P2 打包审计工具，不属于用户运行载荷",
    "scripts/run_p0_final_gate.py": "dev-only: 一次性门禁脚本，文件内写死本机绝对路径",
    # 该文件第 45 行硬编码了 `/Users/<owner>/Documents/Codex/...` 的外部工作区备份路径。
    # 修正它属于该文件所属工作包的职责，本轮**不代改别人的文件**，改为在副本中排除，
    # 并在下面的影响说明里如实登记：副本里**少一个测试用例**。
    "tests/test_multilingual_26_acceptance_gate.py":
        "含硬编码的本机外部工作区绝对路径；源侧问题已上报该文件所属工作包，"
        "本轮不在副本内修改他人文件",
}

#: 白名单（供演示/脱敏用途的字符串，经人工逐条确认**不是**凭证也不是本机真实路径）。
#: 形如 {相对路径: [(规则名, 必须仍出现在文件里的片段, 理由)]}。
#: 之所以要求"片段必须仍在"：一旦对方工作包改动了这行，白名单自动失效并回到 must_fix，
#: 不会变成永久豁免。白名单只影响**副本门禁判定**，不改变下面第 9 节里的源侧问题清单。
ALLOWLIST = {
    "tests/test_scene_images_pexels.py": [
        ("api_key_assign", 'api_key = "test-only-not-a-real-api-key"',
         "测试占位符字符串，文件自述不是真实密钥；无凭证泄漏风险"),
    ],
    "tests/test_semantic_scenes.py": [
        ("abs_user_path", "/Users/example/Library/Application Support/LocalQwenService",
         "文档里的示例路径占位符（/Users/example），非本机真实路径"),
    ],
    "wengao_ppt/scene_model.py": [
        ("abs_private_tmp",
         're.sub(r"(?:/Users/|/Volumes/|/private/var/folders/)[^,);\\n]*"',
         "这是把本机路径从报错文本里抹掉的**脱敏正则**，不是路径泄漏"),
    ],
}


def allowlist_reason(rel: str, rule: str, text: str):
    """命中若与白名单条目（规则+片段）一致且片段仍在文件中，返回理由；否则 None。"""
    for entry_rule, fragment, reason in ALLOWLIST.get(rel, []):
        if entry_rule == rule and fragment in text:
            return reason
    return None

#: 内容级脱敏：必须进副本但携带本机绝对路径的行。
#: key = 相对路径；value = 替换列表 [(原文片段, 替换后, 理由)]
REDACTIONS = {
    "samples/open-source-50-decks/multilingual-inputs/generation-evidence/"
    "batch-25-28-language-reclassification.json": [
        ("/Users/huangyingteng/Documents/Codex/2026-10-20/n-2", PLACEHOLDER,
         "指向本机外部监督工作区的绝对路径"),
    ],
}

#: 资产政策：尚未取得再分发许可 / 缺少生成证据的图片资产。
#: 默认不进对外副本；`--include-unverified-assets` 生成内部副本时保留。
UNVERIFIED_ASSETS = {
    "assets/visual_img/aisle_supermarket.png": "AI 生成资产：缺少可核验的生成证据与 AI 标识元数据",
    "assets/visual_img/desk_planning.png": "AI 生成资产：缺少可核验的生成证据与 AI 标识元数据",
    "assets/visual_img/factory_night.png": "AI 生成资产：缺少可核验的生成证据与 AI 标识元数据",
    "assets/visual_img/promo_ecom.png": "AI 生成资产：缺少可核验的生成证据与 AI 标识元数据",
    "assets/visual_img/skyline_city.png": "AI 生成资产：缺少可核验的生成证据与 AI 标识元数据",
    "assets/visual_img/semantic_scenes/consumer-shopping-4971925.jpeg":
        "Pexels 照片：资产清单自述 NOT_CLEARED_FOR_SOURCE_BUNDLE",
    "assets/visual_img/semantic_scenes/consumer-payment-receipt-3570244.jpeg":
        "Pexels 照片：资产清单自述 NOT_CLEARED_FOR_SOURCE_BUNDLE",
    "assets/visual_img/semantic_scenes/factory-workers-32048366.jpeg":
        "Pexels 照片：资产清单自述 NOT_CLEARED_FOR_SOURCE_BUNDLE",
    "assets/visual_img/semantic_scenes/production-line-modern-34718930.jpeg":
        "Pexels 照片：资产清单自述 NOT_CLEARED_FOR_SOURCE_BUNDLE",
    "assets/visual_img/semantic_scenes/delivery-courier-6868460.jpeg":
        "Pexels 照片：资产清单自述 NOT_CLEARED_FOR_SOURCE_BUNDLE",
}

TEXT_SUFFIXES = scanmod.TEXT_SUFFIXES


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def plan_members(src: Path, include_unverified_assets: bool):
    """返回 [(相对路径, 是否复制, 理由)]，理由为空表示正常成员。"""
    rows = []
    for path in scanmod.iter_release_members(src):
        rel = path.relative_to(src).as_posix()
        if rel in EXTRA_EXCLUDE_FILES:
            rows.append((rel, False, EXTRA_EXCLUDE_FILES[rel]))
        elif not include_unverified_assets and rel in UNVERIFIED_ASSETS:
            rows.append((rel, False, UNVERIFIED_ASSETS[rel]))
        else:
            rows.append((rel, True, ""))
    return rows


def apply_redaction(rel: str, data: bytes):
    """确定性脱敏；返回 (新字节, 命中明细)。"""
    specs = REDACTIONS.get(rel) or []
    if not specs:
        return data, []
    text = data.decode("utf-8")
    hits = []
    for old, new, reason in specs:
        n = text.count(old)
        if n:
            text = text.replace(old, new)
            hits.append({"line_fragment": old, "replaced_with": new,
                         "occurrences": n, "reason": reason})
    return text.encode("utf-8"), hits


def main() -> int:
    ap = argparse.ArgumentParser(description="P2 可发布消毒副本生成器")
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--report", required=True, help="审计/报告输出目录")
    ap.add_argument("--deck-manifest", type=Path,
                    help="真实 50 套发布门禁的 manifest；--apply 时必填")
    ap.add_argument("--deck-freeze-manifest", type=Path,
                    help="与当前候选冻结身份绑定的文件清单；--apply 时必填")
    ap.add_argument("--include-unverified-assets", action="store_true",
                    help="生成内部副本（保留未清许可资产；不得用于对外发布）")
    ap.add_argument("--apply", action="store_true", help="真正写盘；否则只做计划预览")
    args = ap.parse_args()

    src = Path(args.src).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    report_dir = Path(args.report).expanduser().resolve()
    if not src.is_dir():
        print(f"SRC_NOT_FOUND: {src}", file=sys.stderr)
        return 4
    if out.exists() and args.apply:
        print(f"OUT_EXISTS_REFUSING_TO_OVERWRITE: {out}", file=sys.stderr)
        return 4

    deck_gate = {"verdict": "PREVIEW_ONLY", "counted_real_decks": 0}
    if args.apply:
        if not args.deck_manifest or not args.deck_freeze_manifest:
            print("RELEASE_GATE_EVIDENCE_REQUIRED: --apply requires --deck-manifest and "
                  "--deck-freeze-manifest", file=sys.stderr)
            return 5
        try:
            deck_gate = deckgate.audit_deck_manifest(
                args.deck_manifest.expanduser().resolve(),
                args.deck_freeze_manifest.expanduser().resolve(),
            )
        except Exception as exc:
            deck_gate = {"verdict": "BLOCKED", "counted_real_decks": 0,
                         "findings": [{"code": "RELEASE_GATE_EXCEPTION",
                                       "error_type": type(exc).__name__}]}
        if deck_gate.get("verdict") != "PASS" or deck_gate.get("counted_real_decks") != 50:
            print("RELEASE_GATE_BLOCKED: " + json.dumps(deck_gate, ensure_ascii=False),
                  file=sys.stderr)
            return 5

    report_dir.mkdir(parents=True, exist_ok=True)
    plan = plan_members(src, args.include_unverified_assets)
    copied = [r for r in plan if r[1]]
    skipped = [r for r in plan if not r[1]]

    copied_bytes, redaction_ledger = [], []
    for rel, _, _ in sorted(copied):
        s = src / rel
        d = out / rel
        raw = s.read_bytes()
        payload, hits = apply_redaction(rel, raw)
        if args.apply:
            d.parent.mkdir(parents=True, exist_ok=True)
            d.write_bytes(payload)
        copied_bytes.append({"path": rel, "source_sha256": sha256_bytes(raw),
                             "copy_sha256": sha256_bytes(payload),
                             "bytes_before": len(raw), "bytes_after": len(payload)})
        for h in hits:
            redaction_ledger.append(dict(h, path=rel,
                                         source_sha256=sha256_bytes(raw),
                                         copy_sha256=sha256_bytes(payload)))

    # 复制后写权属说明文件
    extra_docs = []
    if args.apply and skipped:
        code = sorted([r for r in skipped if not r[0].startswith("assets/")])
        media = sorted([r for r in skipped if r[0].startswith("assets/")])
        lines = ["# 副本排除清单（每条写明理由与源哈希，不讲空话）", "",
                 f"生成时间：{time.strftime('%Y-%m-%dT%H:%M:%S%z')}", "",
                 "## A. 被排除的源码 / 测试 / 工具文件", "",
                 "| 相对路径 | 源 sha256 | 字节 | 排除理由 |", "| :--- | :--- | ---: | :--- |"]
        for rel, _, reason in code:
            p = src / rel
            det = f"{sha256_file(p)}" if p.is_file() else "MISSING"
            size = p.stat().st_size if p.is_file() else -1
            lines.append(f"| `{rel}` | `{det[:16]}…` | {size} | {reason} |")
        lines += ["", "对副本可用性的实际影响（照实写）：",
                  "- 被排除的是**开发期一次性脚本与打包审计工具**，用户运行链路不依赖它们；",
                  "- `tests/test_multilingual_26_acceptance_gate.py` 因硬编码本机路径被排除，",
                  "  因此**副本里少这一个测试用例**（26 语言名单范围与 50 份材料的门禁用例）。",
                  "  这不是“顺手清理”，而是一次明确的覆盖差异：如需在副本里跑它，",
                  "  请先由该文件所属工作包把硬编码路径改成相对路径或环境变量。", "",
                  "## B. 被排除的图片资产", "",
                  "| 相对路径 | 源 sha256 | 字节 | 排除理由 |", "| :--- | :--- | ---: | :--- |"]
        for rel, _, reason in media:
            p = src / rel
            det = f"{sha256_file(p)}" if p.is_file() else "MISSING"
            size = p.stat().st_size if p.is_file() else -1
            lines.append(f"| `{rel}` | `{det[:16]}…` | {size} | {reason} |")
        lines += ["", "对副本可用性的实际影响（照实写）：",
                  "- `wengao_ppt/visual_plan.py` 的旧关键词素材库按绝对路径读取上述 5 张",
                  "  PNG；资产缺失时该入口在读文件处会抛 `FileNotFoundError`（静态核读结论，",
                  "  未在对外副本上真实运行，理由：本机无可用本机模型与渲染链路）。",
                  "  这正是把它们列为发布阻断项的原因，不是可以静默忽略的差异。",
                  "- `assets/visual_img/semantic_scenes/*.jpeg` 是场景映射用的 5 张 Pexels",
                  "  示例图；其 `semantic_scene_assets.json` 自述 `NOT_CLEARED_FOR_SOURCE_BUNDLE`，",
                  "  整包再分发是否允许尚未经所有者/法务确认。",
                  "- 运行时图库链路本应由用户自备素材或经授权的图库检索提供（见 README",
                  "  「可选的 Pexels 场景照片检索」），不依赖副本内置图片。"]
        extra_docs.append(("ASSET-EXCLUSIONS.md", "\n".join(lines) + "\n"))

    if args.apply:
        for name, body in extra_docs:
            (out / name).write_text(body, encoding="utf-8")

    # ---- 对副本本体复跑同一套扫描（同样是 dry-run 时也要能跑，故用预览模式）---
    if args.apply:
        hits_must, hits_manual, hits_allowed, stat = [], [], [], {"files": 0}
        for p in scanmod.iter_release_members(out):
            rel = p.relative_to(out).as_posix()
            stat["files"] += 1
            try:
                whole = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                whole = ""
            _, hits, _ = scanmod.scan_file(p, out)
            for h in hits or []:
                rec = dict(h, path=rel)
                why = allowlist_reason(rel, h["rule"], whole)
                if why:
                    hits_allowed.append(dict(rec, allow_reason=why,
                                             allow_fragment_still_present=True))
                elif h["severity"] == "must_fix":
                    hits_must.append(rec)
                else:
                    hits_manual.append(rec)
        copy_scan = {"must_fix": hits_must, "manual_review": hits_manual,
                     "allowlisted": hits_allowed,
                     "counts": {"must_fix": len(hits_must),
                                "manual_review": len(hits_manual),
                                "allowlisted": len(hits_allowed),
                                "files": stat["files"]}}
    else:
        copy_scan = {"status": "PREVIEW_ONLY"}

    result = {
        "schema": "P2_RELEASE_COPY_BUILD_V1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source": str(src),
        "output": str(out),
        "mode": "apply" if args.apply else "preview",
        "include_unverified_assets": args.include_unverified_assets,
        "counts": {"copied": len(copied), "skipped": len(skipped)},
        "copied": copied_bytes,
        "skipped": [{"path": r[0], "reason": r[2],
                     "source_sha256": sha256_file(src / r[0]) if (src / r[0]).is_file() else None}
                    for r in sorted(skipped)],
        "redaction_ledger": redaction_ledger,
        "copy_rescan": copy_scan,
        "prepublication_50_decks_gate": deck_gate,
        "copied_manifest_sha256": hashlib.sha256(
            "\n".join(f"{c['copy_sha256']}  {c['path']}" for c in sorted(
                copied_bytes, key=lambda x: x["path"])).encode()).hexdigest(),
    }
    (report_dir / "release_copy_build.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    # 副本 SHA 清单（可传给验收者独立核对）
    lines = ["# P2 消毒副本 SHA256 清单", f"# 源：{src}", f"# 副本：{out}",
             f"# 模式：{result['mode']}", f"# 复制成员数：{len(copied)}", ""]
    for c in sorted(copied_bytes, key=lambda x: x["path"]):
        lines.append(f"{c['copy_sha256']}  {c['bytes_after']:>8}  {c['path']}")
    (report_dir / "RELEASE_COPY_SHAS.txt").write_text("\n".join(lines) + "\n",
                                                      encoding="utf-8")

    print("== BUILD SUMMARY ==")
    print(f"mode                 : {result['mode']}")
    print(f"copied / skipped     : {len(copied)} / {len(skipped)}")
    print(f"redactions applied   : {len(redaction_ledger)}")
    print(f"copied_manifest_sha  : {result['copied_manifest_sha256']}")
    if args.apply:
        print(f"copy files scanned   : {copy_scan['counts'].get('files')}")
        print(f"copy rescan must_fix : {copy_scan['counts']['must_fix']}")
        print(f"copy rescan manual   : {copy_scan['counts']['manual_review']}")
        print(f"copy rescan allowed  : {copy_scan['counts']['allowlisted']}")
        for h in hits_allowed:
            print(f"   [ALLOWED] {h['path']}:{h['line']} {h['rule']} — {h['allow_reason']}")
        for h in hits_must[:20]:
            print(f"   [RESIDUAL-MUST] {h['path']}:{h['line']} {h['rule']}")
        print(f"report               : {report_dir / 'release_copy_build.json'}")
        return 2 if hits_must else 0
    print("(preview only; pass --apply to write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
