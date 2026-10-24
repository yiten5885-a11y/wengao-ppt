#!/usr/bin/env python3
"""P7 工作包：26 种语言真实全链审计（只审计，不签发 verdict）。

设计原则（遵循 AGENTS.md 第 2/3 条与任务书条款 3/4）：
- 只执行**当前环境真实可执行**的层，产出真实运行证据。
- 因外部依赖缺失（模型网关 / 图库授权 / 上游生成产物）而无法执行的层，
  一律输出 BLOCKED 并附真实探测证据，**不得**用 fixture / mock / replay
  / 旧任务 / 自报 PASS 代替。
- 本脚本不修改任何产品源码，不触碰 workbench/server.py。

Layer 定义（与 contracts/language_acceptance_scope_v1.json required_layers 对齐）:
  L1 text_recognition            真实执行（离线检测器 unicode-script-lexical-v4）
  L2 semantic_planning           需模型网关 -> 探测后判定
  L3 visual_query_generation     依赖 L2 -> 探测后判定
  L4 photo_retrieval_insertion   依赖 L3 + 外部图库授权 -> 探测后判定
  L5 typography_and_layout       真实执行（系统字体真实字形覆盖检测）
  L6 generation                  依赖 L2 -> 探测后判定
  L7 per_page_render             依赖 L6 + 渲染器 -> 探测后判定
  L8 workbench_preview           依赖 L7 -> 探测后判定
  L9 download                    依赖 L8 -> 探测后判定
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import socket
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MANIFEST = REPO_ROOT / "samples" / "open-source-50-decks" / "multilingual-inputs" / "input-manifest.json"
MATERIAL_DIR = REPO_ROOT / "samples" / "open-source-50-decks" / "multilingual-inputs"

FONT_DIRS = [
    "/System/Library/Fonts",
    "/System/Library/Fonts/Supplemental",
    "/Library/Fonts",
    os.path.expanduser("~/Library/Fonts"),
]
FONT_EXT = ("*.ttf", "*.ttc", "*.otf", "*.otc")

MODEL_ENDPOINTS = [
    ("http://127.0.0.1:11434", "/api/version"),
    ("http://127.0.0.1:18888", "/health"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- 环境探测
def probe_model_gateway() -> dict:
    """真实探测模型网关：socket 层 + HTTP 层。不重启、不改写任何共享服务。"""
    out = {"checked_at_utc": now_iso(), "endpoints": [], "available": False}
    saved = {k: os.environ.pop(k, None) for k in
             ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy")}
    try:
        import urllib.request
        for base, path in MODEL_ENDPOINTS:
            host = base.split("://")[1].split(":")[0]
            port = int(base.split(":")[-1])
            rec = {"base": base, "probe_path": path}
            s = socket.socket()
            s.settimeout(3)
            try:
                s.connect((host, port))
                rec["tcp_connect"] = "OK"
            except Exception as e:
                rec["tcp_connect"] = f"FAIL:{type(e).__name__}:{e}"
            finally:
                s.close()
            try:
                with urllib.request.urlopen(urllib.request.Request(base + path), timeout=8) as r:
                    rec["http_status"] = r.status
                    rec["http_body_head"] = r.read(200).decode("utf-8", "replace")
            except Exception as e:
                rec["http_error"] = f"{type(e).__name__}:{str(e)[:200]}"
            out["endpoints"].append(rec)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    out["available"] = any(e.get("tcp_connect") == "OK" for e in out["endpoints"])
    out["note"] = "只读探测；未重启或改写任何共享 Ollama / 代理 / CLI"
    return out


def probe_renderer() -> dict:
    """真实探测渲染器是否存在（不做渲染）。"""
    cand = [
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "/usr/local/bin/soffice",
        "/opt/homebrew/bin/soffice",
    ]
    found = [c for c in cand if os.path.exists(c)]
    return {"checked_at_utc": now_iso(), "renderer_binary_present": bool(found),
            "found": found, "note": "仅做存在性探测，未执行渲染"}


def probe_photo_library_authorization() -> dict:
    """图片检索授权状态：本工作包不持有任何图库凭据，且任务书要求先确认授权。

    因此此项一律记为 NOT_AUTHORIZED_IN_THIS_PACKAGE（真实状态，非推断）。
    """
    keys = [k for k in os.environ if any(t in k.upper() for t in
            ("PEXEL", "UNSPLASH", "PIXABAY", "IMAGE_API", "STOCK_API"))]
    return {
        "checked_at_utc": now_iso(),
        "credential_env_keys_found": keys,
        "authorized": False,
        "status": "NOT_AUTHORIZED_IN_THIS_PACKAGE",
        "note": "未发现已授权的图库凭据；按条款 2 不得自行发起外部图库请求或付费",
    }


# ---------------------------------------------------------------- 层 1：识别
def run_l1_recognition(entries: list[dict]) -> dict:
    from wengao_ppt.language_support import (
        CURRENT_LANGUAGE_ACCEPTANCE_CODES,
        LANGUAGE_DETECTOR_VERSION,
        detect_language_hint,
        is_cjk_language,
        is_rtl_language,
    )
    per_case = []
    for e in entries:
        fname = os.path.basename(e["source_path"])
        path = MATERIAL_DIR / fname
        rec = {"case_id": e["case_id"], "file": fname,
               "declared_language": e["source_language"]}
        if not path.exists():
            rec.update(layer="L1", status="BLOCKED", reason="material file missing")
            per_case.append(rec)
            continue
        raw = path.read_text(encoding="utf-8")
        blob = raw.encode("utf-8")
        rec["input_sha256"] = hashlib.sha256(blob).hexdigest()
        rec["input_bytes"] = len(blob)
        rec["manifest_sha256_matches"] = (rec["input_sha256"] == e.get("input_sha256"))
        hint = detect_language_hint(raw)
        rec["detected_language"] = hint.get("primary")
        rec["detected_scripts"] = hint.get("scripts")
        rec["detector_version"] = hint.get("detector", LANGUAGE_DETECTOR_VERSION)
        rec["detector_confidence"] = hint.get("confidence")
        rec["detector_mixed"] = hint.get("mixed")
        rec["detector_uncertain"] = hint.get("uncertain")
        rec["detector_raw_keys"] = sorted(hint.keys())
        rec["is_rtl"] = is_rtl_language(e["source_language"])
        rec["is_cjk"] = is_cjk_language(e["source_language"])
        rec["detection_matches_declared"] = (
            rec["detected_language"] == e["source_language"])
        rec["status"] = "PASS" if rec["detection_matches_declared"] else "FAIL"
        per_case.append(rec)
    return {"detector_version": LANGUAGE_DETECTOR_VERSION,
            "scope_codes": list(CURRENT_LANGUAGE_ACCEPTANCE_CODES),
            "per_case": per_case}


# ---------------------------------------------------------------- 层 5：字体
def _load_fonts() -> list[Path]:
    files: list[Path] = []
    for d in FONT_DIRS:
        if not os.path.isdir(d):
            continue
        for ext in FONT_EXT:
            files.extend(Path(p) for p in glob.glob(os.path.join(d, ext)))
    return sorted(files)


def _build_coverage_sets(font_files: list[Path], charset: list[str],
                         probe_chars: int = 10 ** 9) -> dict[str, set]:
    """用 PIL 真实渲染判定字形覆盖。

    判定方法：把候选字符与目标字体渲染的 mask，与必然缺失字符 U+FFFF 的
    mask 做比对；一致（或零面积）视为该字体未覆盖此字符。这是真实渲染检测，
    不使用任何预置清单。
    """
    from PIL import ImageFont
    covered: dict[str, set] = defaultdict(set)
    missing_marker = "\uFFFF"
    tested = 0
    for fp in font_files:
        try:
            font = ImageFont.truetype(str(fp), 32)
        except Exception:
            continue
        try:
            ref = font.getmask(missing_marker)
            ref_bbox = font.getbbox(missing_marker)
        except Exception:
            continue
        ref_bytes = ref.tobytes() if hasattr(ref, "tobytes") else bytes(ref)
        for ch in charset:
            if tested >= probe_chars * len(font_files):
                break
            tested += 1
            try:
                bbox = font.getbbox(ch)
                mask = font.getmask(ch)
            except Exception:
                continue
            mbytes = mask.tobytes() if hasattr(mask, "tobytes") else bytes(mask)
            zero_area = (bbox[2] - bbox[0] <= 0) or (bbox[3] - bbox[1] <= 0)
            same_as_missing = (mbytes == ref_bytes) and (bbox == ref_bbox)
            if not zero_area and not same_as_missing:
                covered[ch].add(fp.name)
    return covered


def _extract_language_chars(paths: list[Path], limit: int = 60) -> list[str]:
    """从真实材料中提取该语言使用的非 ASCII 字母字符（去重，按出现频次）。"""
    counter: Counter = Counter()
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        for ch in text:
            if ch.isspace():
                continue
            if ord(ch) < 128:
                continue
            if unicodedata.category(ch).startswith("L") or unicodedata.category(ch).startswith("M"):
                counter[ch] += 1
    return [c for c, _ in counter.most_common(limit)]


def run_l5_typography(entries: list[dict], font_files: list[Path]) -> dict:
    by_lang: dict[str, list[Path]] = defaultdict(list)
    for e in entries:
        p = MATERIAL_DIR / os.path.basename(e["source_path"])
        if p.exists():
            by_lang[e["source_language"]].append(p)

    # 一次性构建全局覆盖表：只加载每个字体一次，避免 26 次重复加载。
    lang_charset: dict[str, list[str]] = {}
    union: list[str] = []
    seen: set[str] = set()
    for lang in sorted(by_lang):
        cs = _extract_language_chars(by_lang[lang], limit=60)
        lang_charset[lang] = cs
        for c in cs:
            if c not in seen:
                seen.add(c)
                union.append(c)
    print(f"[p7] 字体覆盖：{len(font_files)} 字体 × {len(union)} 唯一字符 ...")
    covered = _build_coverage_sets(font_files, union)

    per_lang = {}
    for lang in sorted(by_lang):
        paths = by_lang[lang]
        charset = lang_charset[lang]
        if not charset:
            per_lang[lang] = {"status": "PASS", "note": "该语言材料为纯拉丁字符",
                              "charset_size": 0, "covered": 0, "coverage_ratio": 1.0,
                              "sample_files": [p.name for p in paths]}
            continue
        hit = sum(1 for c in charset if covered.get(c))
        ratio = hit / len(charset)
        per_lang[lang] = {
            "charset_size": len(charset),
            "covered": hit,
            "uncovered": len(charset) - hit,
            "coverage_ratio": round(ratio, 4),
            "status": "PASS" if ratio == 1.0 else ("PARTIAL" if ratio >= 0.9 else "FAIL"),
            "uncovered_sample_chars": [c for c in charset if not covered.get(c)][:15],
            "sample_files": [p.name for p in paths],
            "fonts_probed": len(font_files),
        }
    return {"method": "PIL 真实渲染判定（与 U+FFFF 缺失字形比对）",
            "font_files_found": len(font_files),
            "per_language": per_lang}


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default=str(REPO_ROOT / "reports" / "p7_lang26"))
    args = ap.parse_args()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    entries = manifest["entries"]
    scope = json.loads(
        (REPO_ROOT / "contracts" / "language_acceptance_scope_v1.json").read_text(encoding="utf-8"))
    scope_codes = scope["acceptance_language_codes"]

    print("[p7] 探测模型网关 ...")
    model_probe = probe_model_gateway()
    print("[p7] 探测渲染器 ...")
    render_probe = probe_renderer()
    print("[p7] 探测图库授权 ...")
    photo_probe = probe_photo_library_authorization()

    print("[p7] L1 语言识别（真实离线检测）...")
    l1 = run_l1_recognition(entries)

    print("[p7] 枚举系统字体 ...")
    fonts = _load_fonts()
    print(f"[p7] L5 字体覆盖真实检测（{len(fonts)} 个字体文件）...")
    l5 = run_l5_typography(entries, fonts)

    model_ok = model_probe["available"]
    blocker_reason = {
        "L2": None if model_ok else "模型网关不可用（详见 model_gateway_probe）",
        "L3": None if model_ok else "依赖 L2 语义规划，模型网关不可用",
        "L4": "未获得外部图库授权（详见 photo_library_probe）",
        "L6": None if model_ok else "依赖 L2 语义规划，模型网关不可用",
        "L7": None if model_ok else "依赖 L6 产物；且本轮未获得可渲染 PPTX 产物",
        "L8": None if model_ok else "依赖 L7 逐页渲染产物",
        "L9": None if model_ok else "依赖 L8 预览产物",
    }

    # 组装 26 × 9 矩阵
    l1_by_lang: dict[str, list] = defaultdict(list)
    for r in l1["per_case"]:
        l1_by_lang[r["declared_language"]].append(r)

    matrix = []
    for code in scope_codes:
        rows = l1_by_lang.get(code, [])
        l1_status = "NOT_RUN"
        if rows:
            l1_status = "PASS" if all(r.get("status") == "PASS" for r in rows) else "FAIL"
        l5info = l5["per_language"].get(code, {})
        row = {
            "language_code": code,
            "material_cases": [r["case_id"] for r in rows],
            "material_count": len(rows),
            "is_rtl": rows[0]["is_rtl"] if rows else None,
            "is_cjk": rows[0]["is_cjk"] if rows else None,
            "L1_text_recognition": l1_status,
            "L2_semantic_planning": "BLOCKED" if not model_ok else "NOT_RUN",
            "L3_visual_query_generation": "BLOCKED" if not model_ok else "NOT_RUN",
            "L4_photo_retrieval_insertion": "BLOCKED",
            "L5_typography_and_layout": l5info.get("status", "NOT_RUN"),
            "L6_generation": "BLOCKED" if not model_ok else "NOT_RUN",
            "L7_per_page_render": "BLOCKED" if not model_ok else "NOT_RUN",
            "L8_workbench_preview": "BLOCKED" if not model_ok else "NOT_RUN",
            "L9_download": "BLOCKED" if not model_ok else "NOT_RUN",
            "L5_detail": l5info,
            "L1_detail": rows,
            "full_chain_complete": False,
        }
        matrix.append(row)

    # 收紧口径：字体资源覆盖（L5a）不等于产品排版链路验收（L5b）。
    # L5b 需要真实生成产物（RTL 方向、复杂文字换行、混排、长文本），
    # 在模型网关不可用时无产物可验，一律记 BLOCKED，不得用 L5a 顶替。
    for m in matrix:
        m["L5_typography_and_layout"] = "BLOCKED"
        m["L5_note"] = ("L5a 字体资源覆盖已真实检测（见 L5_detail）；"
                        "L5b 产品排版链路（RTL 方向/复杂文字换行/混排/长文本）"
                        "需真实生成产物方可验证，本轮无产物，记 BLOCKED")

    l1_pass = sum(1 for m in matrix if m["L1_text_recognition"] == "PASS")
    l5_pass = sum(1 for m in matrix if m["L5_typography_and_layout"] == "PASS")
    l5a_pass = sum(1 for m in matrix
                   if m["L5_detail"].get("status") == "PASS")
    full = sum(1 for m in matrix if m["full_chain_complete"])

    report = {
        "package": "P7-26-language-real-chain-acceptance",
        "generated_at_utc": now_iso(),
        "candidate": "wengao-visual-v13-material-fix",
        "repo_root": str(REPO_ROOT),
        "scope_contract_sha256": sha256_file(REPO_ROOT / "contracts" / "language_acceptance_scope_v1.json"),
        "scope_id": scope.get("scope_id"),
        "scope_language_count": len(scope_codes),
        "environment_probes": {
            "model_gateway": model_probe,
            "renderer": render_probe,
            "photo_library": photo_probe,
        },
        "blockers": blocker_reason,
        "summary": {
            "languages_in_scope": len(scope_codes),
            "L1_recognition_pass": f"{l1_pass}/{len(scope_codes)}",
            "L5a_font_resource_coverage_pass": f"{l5a_pass}/{len(scope_codes)}",
            "L5b_product_layout_render_pass": f"{l5_pass}/{len(scope_codes)}",
            "real_full_chain_complete": f"{full}/{len(scope_codes)}",
            "verdict": "NOT_ACCEPTED",
            "verdict_reason": (
                "模型网关不可用导致 L2/L3/L6-L9 无法真实执行；"
                "外部图库未授权导致 L4 无法真实执行；"
                "L5 仅完成字体资源覆盖子系统检测，产品排版链路（RTL 方向、"
                "复杂文字换行、混排、长文本）无真实产物可验，记 BLOCKED。"
                "按任务书条款 3/4，缺证据一律记为未验证/阻断，不使用 fixture 或自报 PASS 顶替。"),
            "scope_note": (
                "L1/L5a 为真实执行的子系统级证据，不构成任何语言的「全链通过」；"
                "全链通过要求 L1-L9 全部具备真实证据。"),
        },
        "matrix": matrix,
    }

    out_path = outdir / "language_chain_audit.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[p7] 写入 {out_path}")
    print(json.dumps(report["summary"], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
