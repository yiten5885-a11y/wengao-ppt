"""tests/ui/test_ui_static.py —— 新工作台界面静态验收（RS-9 硬约束）。

不启动浏览器也能守住的红线：
- 不依赖 CDN（离线可用）：无外部 http(s) 资源引用
- 关键结构与状态容器齐全（空态/运行/完成/待确认/降级/失败）
- 可访问性：焦点环、aria、lang=zh-CN、prefers-reduced-motion
- 中文优先：不出现内部错误码（T1–T4 / PIPE.* / LAYOUT.* 等）
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "workbench" / "ui"

HTML = (UI / "index.html").read_text(encoding="utf-8")
CSS = (UI / "styles.css").read_text(encoding="utf-8")
APP = (UI / "app.js").read_text(encoding="utf-8")
RENDER = (UI / "render.js").read_text(encoding="utf-8")
ACTIONS = (UI / "actions.js").read_text(encoding="utf-8")
ALL_JS = APP + RENDER + ACTIONS


def test_no_cdn_dependencies():
    for name, text in (("index.html", HTML), ("styles.css", CSS),
                       ("app.js", APP), ("render.js", RENDER),
                       ("actions.js", ACTIONS)):
        for m in re.finditer(r"(?:src|href)\s*=\s*[\"']https?://", text):
            assert False, f"{name} 引用外部资源（离线可用被破坏）：{m.group(0)}"
        assert "fonts.googleapis" not in text, f"{name} 引用 Google Fonts"
        assert "cdn." not in text, f"{name} 疑似引用 CDN"


def test_required_containers_exist():
    for el in ("job-list", "localrun-rows", "mid-scroll", "chat", "empty-state",
               "msg", "btn-send", "btn-import", "btn-research", "btn-lang",
               "tabs", "tab-preview", "tab-sources", "tab-images",
               "preview-img", "thumbs", "page-check-rows", "source-list",
               "image-grid", "toast", "btn-new", "btn-export", "btn-share",
               "btn-history", "btn-transition", "btn-play", "search-input"):
        assert f'id="{el}"' in HTML, f"缺少容器 #{el}"


def test_accessibility_basics():
    assert 'lang="zh-CN"' in HTML
    assert "aria-label" in HTML
    assert "role=\"status\"" in HTML and "aria-live" in HTML  # toast
    assert "focus-visible" in CSS and "outline" in CSS
    assert "prefers-reduced-motion" in CSS
    assert ".sr-only" in CSS  # 屏幕阅读器专用文本


def test_no_internal_codes_in_user_text():
    """界面文案不出现内部错误码/层级代号（中文优先、说人话）。"""
    for name, text in (("index.html", HTML), ("render.js", RENDER),
                       ("actions.js", ACTIONS)):
        for code in ("T1", "T2", "T3", "T4", "PIPE.", "LAYOUT.", "RESEARCH.",
                     "SCENE.", "FALLBACK_", "RESOLVED_"):
            if code in ("T1", "T2", "T3", "T4"):
                # 这些代号只允许出现在注释里
                for m in re.finditer(rf"\b{code}\b", text):
                    line = text[text.rfind("\n", 0, m.start()) + 1:
                                text.find("\n", m.end())]
                    assert line.strip().startswith(("//", "*", "/*")), \
                        f"{name} 文案出现内部代号 {code}：{line.strip()[:60]}"
            else:
                assert code not in text, f"{name} 文案出现内部码 {code}"


def test_js_modules_load_order():
    assert HTML.index("/ui/app.js") < HTML.index("/ui/render.js") < HTML.index("/ui/actions.js")


def test_eight_steps_defined():
    for step in ("read", "research", "compare", "trend", "images",
                 "layout", "transition", "check"):
        assert step in RENDER, f"时间线缺少步骤 {step}"
