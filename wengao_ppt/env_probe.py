"""wengao_ppt.env_probe —— 新增依赖的本机探测（W4 指令 §9）。

原则：面板只显示**真实探测结果**；探测不到就如实说"未探测到"，不猜、不默认安装。
新增依赖前先报告用户（不下载未批准的大模型/大软件）——本模块只探测，不安装。

探测项：
- searxng     ：本机自托管检索（固定 127.0.0.1:8890，W3）
- comfyui     ：本地生图（默认 127.0.0.1:8188，W1），并列出可用 checkpoint
- vision_model：图文相似度模型（Ollama 上的 clip/siglip 类，W1）
- fonts       ：中文渲染字体（PingFang / Noto Sans SC / Source Han 等）
- libreoffice ：页面渲染（render.find_soffice）
- ollama      ：语义模型服务（/api/version + /api/tags）
"""
from __future__ import annotations

import json
from typing import Optional

from wengao_ppt.netsafe import safe_urlopen

TIMEOUT_S = 4


def _get_json(url: str) -> dict:
    return json.loads(safe_urlopen(url, allow_loopback=True, timeout_s=TIMEOUT_S).decode("utf-8"))


def probe_searxng() -> dict:
    row = {"key": "searxng", "label": "联网检索（SearXNG）", "ok": False,
           "detail": "未探测到 127.0.0.1:8890"}
    try:
        _get_json("http://127.0.0.1:8890/stats")
        row.update(ok=True, detail="SearXNG 本机自托管（8890）")
    except Exception as e:  # noqa: BLE001
        row["detail"] = f"未探测到（{type(e).__name__}）"
    return row


def probe_comfyui() -> dict:
    row = {"key": "comfyui", "label": "本地生图（ComfyUI）", "ok": False,
           "detail": "未探测到 127.0.0.1:8188", "checkpoints": []}
    try:
        stats = _get_json("http://127.0.0.1:8188/system_stats")
        row["ok"] = True
        dev = ((stats.get("devices") or [{}])[0]).get("name", "")
        row["detail"] = f"ComfyUI 已运行（{dev or '本机'}）"
        try:
            info = _get_json("http://127.0.0.1:8188/object_info/CheckpointLoaderSimple")
            ckpts = ((info.get("CheckpointLoaderSimple") or {}).get("input") or {}) \
                .get("required", {}).get("ckpt_name") or []
            row["checkpoints"] = list(ckpts)[:10]
            if row["checkpoints"]:
                row["detail"] += f"，{len(row['checkpoints'])} 个 checkpoint"
        except Exception:  # noqa: BLE001 —— 型号列表拿不到不影响"已运行"结论
            pass
    except Exception as e:  # noqa: BLE001
        row["detail"] = f"未探测到（{type(e).__name__}）"
    return row


def probe_vision_model() -> dict:
    row = {"key": "vision_model", "label": "视觉模型（图文相似度）", "ok": False,
           "detail": "未探测到 clip/siglip 类模型"}
    try:
        tags = _get_json("http://127.0.0.1:11434/api/tags")
        names = [m.get("name", "") for m in tags.get("models") or []]
        hits = [n for n in names
                if any(k in n.lower() for k in ("clip", "siglip", "vision"))]
        if hits:
            row.update(ok=True, detail=f"已就位：{'、'.join(hits[:3])}", models=hits)
    except Exception as e:  # noqa: BLE001
        row["detail"] = f"探测失败（{type(e).__name__}）"
    return row


def probe_fonts() -> dict:
    row = {"key": "fonts", "label": "中文字体", "ok": False, "detail": "未找到中文字体",
           "found": []}
    candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
        "/Library/Fonts/Noto Sans CJK SC Regular.otf",
        "/Library/Fonts/SourceHanSansSC-Regular.otf",
        str(__import__("pathlib").Path.home() / "Library/Fonts/NotoSansSC-Regular.otf"),
    ]
    from pathlib import Path
    found = [c for c in candidates if Path(c).is_file()]
    if found:
        row.update(ok=True, found=found,
                   detail=f"已找到 {len(found)} 个中文字体（渲染中文页面需要至少一个）")
    return row


def probe_libreoffice() -> dict:
    row = {"key": "libreoffice", "label": "页面渲染（LibreOffice）", "ok": False,
           "detail": "未找到 LibreOffice"}
    try:
        from wengao_ppt import render as render_mod
        soffice, src = render_mod.find_soffice()
        if soffice:
            row.update(ok=True, detail=f"LibreOffice（{src}）")
    except Exception as e:  # noqa: BLE001
        row["detail"] = f"探测失败（{type(e).__name__}）"
    return row


def probe_ollama() -> dict:
    row = {"key": "ollama", "label": "语义模型（Ollama）", "ok": False,
           "detail": "未探测到 127.0.0.1:11434", "models": []}
    try:
        ver = _get_json("http://127.0.0.1:11434/api/version").get("version", "")
        tags = _get_json("http://127.0.0.1:11434/api/tags")
        names = [m.get("name", "") for m in tags.get("models") or []]
        row.update(ok=True, detail=f"Ollama {ver}，{len(names)} 个模型", models=names[:10])
    except Exception as e:  # noqa: BLE001
        row["detail"] = f"未探测到（{type(e).__name__}）"
    return row


def probe_all() -> dict:
    return {"schema": "rs.env_probe.v1",
            "rows": [probe_ollama(), probe_searxng(), probe_libreoffice(),
                     probe_comfyui(), probe_vision_model(), probe_fonts()],
            "note": "只探测不安装；新增依赖前先报告用户。"}


def as_text(report: Optional[dict] = None) -> str:
    rep = report or probe_all()
    lines = ["本机依赖探测（只探测，不安装）："]
    for r in rep["rows"]:
        mark = "✓" if r["ok"] else "✗"
        lines.append(f"  {mark} {r['label']}：{r['detail']}")
    return "\n".join(lines)
