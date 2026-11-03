#!/usr/bin/env python3
"""把同一份原稿分别套上 6 个主题，真渲染、逐页截图、量化差异，产出风格画廊。

为什么需要它
------------
"6 个主题 6 个风格"这句话在注册表里可以写得很漂亮，但只有**渲染出来的页面**能证明
它成立。此前 basic 与 business 的差异只是 12° 色相（见 theme.py 的 1.2.0 注释），
在代码里看不出来，在页面上也看不出来；把两版都渲染出来、量一下像素差，问题立刻暴露。

做法
----
1. 对每个主题调用 `theme.apply_theme()`（真实应用器，不是模拟），拿到身份与变更统计；
2. 用项目自己的 `render.render_pages()`（真实 LibreOffice）转 PDF 再逐页 PNG；
3. 用 PIL 逐页算两两之间的平均像素差（0=完全相同，1=全黑对全白）；
4. 输出 GALLERY.json（可复核）+ gallery.html（给人看）。

边界
----
* 这里的对比度是**名义**对比度（色值公式），渲染抗锯齿后的实测由 layout_gate 复核；
* LibreOffice 渲染不等于 PowerPoint 兼容性验收，见 reports 里既有声明；
* 自适应主题走 refine 路径（保原稿设计），它的"不同"来自策略而非配色。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from wengao_ppt import render as render_mod  # noqa: E402
from wengao_ppt import theme as theme_mod  # noqa: E402


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _renderer_id() -> str:
    """渲染器身份：写进证据，避免"用哪个 LibreOffice 渲的"变成回忆。"""
    # find_soffice() 返回 (路径, 来源) 元组，不是裸路径——直接拿去跑 --version 会抛异常，
    # 于是渲染器身份被写成"版本未知"（第一版就这样，读数时才发现）。
    found = render_mod.find_soffice()
    soffice = found[0] if isinstance(found, tuple) and found else found
    if not soffice:
        return "LibreOffice（未找到）"
    try:
        v = render_mod.soffice_version(soffice)
        return (v or "").strip().splitlines()[0] if (v or "").strip() else "LibreOffice（版本未知）"
    except Exception as exc:  # noqa: BLE001
        return f"LibreOffice（版本读取失败：{type(exc).__name__}）"


def _swatches(tid: str) -> list[str]:
    """色卡直接取注册表里的真实取值（不在这里另编一份）；自适应主题为空。"""
    t = theme_mod.THEMES[tid]
    if t.get("adaptive") or not t.get("colors"):
        return []
    c = t["colors"]
    return [t["slide_bg"], c["primary"], c["accent"], c["surface"]]


def _load(path: Path) -> "np.ndarray":
    import numpy as np
    return np.asarray(Image.open(path).convert("RGB"), dtype=np.float32)


def _background(arr: "np.ndarray") -> "np.ndarray":
    """一页的"底色"：出现次数最多的颜色（浅色主题可能是白，深色主题是近黑）。"""
    import numpy as np
    flat = arr.reshape(-1, 3).astype(np.uint8)
    packed = (flat[:, 0].astype(np.uint32) << 16) | (flat[:, 1].astype(np.uint32) << 8) | flat[:, 2]
    vals, counts = np.unique(packed, return_counts=True)
    v = int(vals[counts.argmax()])
    return np.array([(v >> 16) & 255, (v >> 8) & 255, v & 255], dtype=np.float32)


def _hist(arr: "np.ndarray", bins: int = 4) -> "np.ndarray":
    """粗色彩直方图（每通道 bins 档），用于衡量"配色构成"而不是"改动面积"。"""
    import numpy as np
    q = np.clip((arr / 256.0 * bins).astype(np.int32), 0, bins - 1)
    idx = (q[..., 0] * bins + q[..., 1]) * bins + q[..., 2]
    h = np.bincount(idx.ravel(), minlength=bins ** 3).astype(np.float64)
    return h / max(1.0, h.sum())


def compare_pages(a: Path, b: Path) -> dict:
    """比较同一页的两张渲染图。

    为什么不是单一"整页平均像素差"：它按面积加权，而两套浅色主题的差异几乎全在
    文字与细线上（面积占比个位数）——于是 basic 与 adaptive 这种"排版/字体全不同"
    的组合会算出 0.004，看起来像没差别。所以给三个互补的数：

    * page_diff  整页平均绝对差（"这一页改了多少"）
    * ink_diff   只在**内容像素**上算（任一张图偏离它自己底色的像素），
                 不被大片留白稀释，才是"风格差"
    * hist_dist  4×4×4 色彩直方图的 L1/2 距离（"配色构成差"，0=同分布，1=完全不重叠）
    """
    import numpy as np
    ia, ib = _load(a), _load(b)
    if ia.shape != ib.shape:
        ib = np.asarray(Image.open(b).convert("RGB").resize((ia.shape[1], ia.shape[0])),
                        dtype=np.float32)
    d = np.abs(ia - ib)
    page_diff = float(d.mean() / 255.0)

    bga, bgb = _background(ia), _background(ib)
    mask = (np.abs(ia - bga).max(axis=2) > 12) | (np.abs(ib - bgb).max(axis=2) > 12)
    ink_area = float(mask.mean())
    ink_diff = float(d[mask].mean() / 255.0) if mask.any() else 0.0

    ha, hb = _hist(ia), _hist(ib)
    hist_dist = float(np.abs(ha - hb).sum() / 2.0)
    return {"page_diff": round(page_diff, 4), "ink_diff": round(ink_diff, 4),
            "hist_dist": round(hist_dist, 4), "ink_area": round(ink_area, 4)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="6 个主题的真实渲染对照与差异量化")
    ap.add_argument("--source", required=True, help="原稿 PPTX（同一份给所有主题）")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--dpi", type=int, default=96)
    ap.add_argument("--themes", default="", help="逗号分隔；缺省为全部注册主题")
    ap.add_argument("--max-pages", type=int, default=0, help="只渲染前 N 页（0=全部）")
    a = ap.parse_args(argv)

    src = Path(a.source).expanduser().resolve()
    if not src.is_file():
        print(json.dumps({"ok": False, "detail": f"原稿不存在：{src}"}, ensure_ascii=False))
        return 4
    out = Path(a.out).expanduser().resolve()
    (out / "pptx").mkdir(parents=True, exist_ok=True)
    (out / "render").mkdir(parents=True, exist_ok=True)

    ids = [t.strip() for t in a.themes.split(",") if t.strip()] or list(theme_mod.THEMES)
    records, failures = [], []

    for tid in ids:
        ident = theme_mod.resolve_theme(tid)
        dst = out / "pptx" / f"{tid}.pptx"
        try:
            stats = theme_mod.apply_theme(str(src), str(dst), tid)
        except Exception as exc:  # noqa: BLE001
            failures.append({"theme": tid, "stage": "apply", "error": f"{type(exc).__name__}: {exc}"})
            continue
        rdir = out / "render" / tid
        # 必须先清空：render_pages 往目录里写 page-XX.png，旧文件不会自己消失，
        # 上一份（页数更多的）原稿留下的 page-07..13 会被当成本次产物混进统计。
        # 这不是假设——换源稿后就出现过"6 页原稿报 13 页"。
        if rdir.exists():
            shutil.rmtree(rdir)
        r = render_mod.render_pages(str(dst), str(rdir), dpi=a.dpi)
        pngs = [Path(p) for p in (r.get("pngs") or [])]
        if a.max_pages:
            pngs = pngs[: a.max_pages]
        if not r.get("ok") or not pngs:
            failures.append({"theme": tid, "stage": "render",
                             "error": r.get("code") or "no png", "detail": r.get("detail")})
            continue
        # pdf_page_count 返回 (页数, 错误)，失败时第一项是 None —— 不能拿元组直接比
        pdf_pages, pdf_err = (render_mod.pdf_page_count(Path(r["pdf"]))
                              if r.get("pdf") else (None, "无 PDF"))
        if pdf_pages is not None and pdf_pages != len(pngs):
            failures.append({"theme": tid, "stage": "render",
                             "error": "page-count-mismatch",
                             "detail": f"PDF {pdf_pages} 页，PNG {len(pngs)} 张（产物目录可能有残留）"})
            continue
        records.append({
            "theme": tid,
            "name": ident["name"],
            "style": theme_mod.THEMES[tid].get("style") or {},
            "version": ident["theme_version"],
            "config_digest": ident["config_digest"],
            "application": stats.get("theme_application"),
            "pptx": str(dst.relative_to(out)),
            "pptx_sha256": sha256(dst),
            "pages_rendered": len(pngs),
            "page_pngs": [str(p.relative_to(out)) for p in pngs],
            # 页面构件：这是"换色"与"换风格"的分界，必须进证据而不是只写在文档里。
            # count 为实际画上去的图元数，skipped 为被避让规则挡下的（挡住内容就不画）。
            "furniture": stats.get("furniture") or {},
            "contrast_claim": stats.get("contrast_claim"),
            "min_ratio": (stats.get("contrast") or {}).get("min_ratio"),
            # 色卡直接取注册表里的真实取值（不在这里另编一份）；自适应主题为空
            "swatches": _swatches(tid),
        })
        print(f"[OK] {tid}: {len(pngs)} 页, application={stats.get('theme_application')}")

    pairwise = {}
    if len(records) >= 2:
        for i in range(len(records)):
            for j in range(i + 1, len(records)):
                a_rec, b_rec = records[i], records[j]
                n = min(len(a_rec["page_pngs"]), len(b_rec["page_pngs"]))
                per = [compare_pages(out / a_rec["page_pngs"][k], out / b_rec["page_pngs"][k])
                       for k in range(n)]
                agg = {k: round(sum(p[k] for p in per) / n, 4)
                       for k in ("page_diff", "ink_diff", "hist_dist", "ink_area")}
                identical = sum(1 for p in per if p["ink_diff"] == 0.0)
                pairwise[f"{a_rec['theme']}|{b_rec['theme']}"] = {
                    **agg, "identical_pages": identical, "per_page": per,
                }

    weakest = min(pairwise.items(), key=lambda kv: kv[1]["ink_diff"]) if pairwise else None
    payload = {
        "schema": "wengao.theme_gallery.v1",
        "scope": "同一原稿 × 6 主题的真实渲染对照；LibreOffice 渲染 ≠ PowerPoint 兼容性",
        "source": str(src),
        "source_sha256": sha256(src),
        "dpi": a.dpi,
        "renderer": _renderer_id(),
        "themes": records,
        "pairwise_page_diff": pairwise,
        "weakest_pair": {"pair": weakest[0], "ink_diff": weakest[1]["ink_diff"],
                         "page_diff": weakest[1]["page_diff"],
                         "hist_dist": weakest[1]["hist_dist"]} if weakest else None,
        "failures": failures,
        "verdict": "PASS" if records and not failures else "INCOMPLETE",
    }
    (out / "GALLERY.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    (out / "gallery.html").write_text(_html(payload), encoding="utf-8")
    print(json.dumps({"themes": len(records), "failures": failures,
                      "weakest_by_ink_diff": payload["weakest_pair"]},
                     ensure_ascii=False, indent=2))
    return 0 if records and not failures else 2


def _html(d: dict) -> str:
    cards = []
    for rec in d["themes"]:
        st = rec["style"] or {}
        sw = "".join(f"<i style='background:#{c}'></i>" for c in rec.get("swatches") or [])
        pages = "".join(
            f"<img src='{p}' alt='{rec['theme']} 第 {k+1} 页' loading='lazy'>"
            for k, p in enumerate(rec["page_pngs"][:3]))
        cards.append(f"""
    <article class="card">
      <h3>{st.get('style_name', rec['name'])} <small>{st.get('genre','')}</small></h3>
      <p class="tag">{st.get('tagline','')}</p>
      <div class="sw">{sw}</div>
      <div class="shot">{pages}</div>
      <dl>
        <dt>主题</dt><dd>{rec['theme']} · v{rec['version']}</dd>
        <dt>应用</dt><dd>{rec['application']} · {rec['pages_rendered']} 页</dd>
        <dt>摘要</dt><dd class="mono">{rec['config_digest'][:16]}…</dd>
      </dl>
    </article>""")
    rows = "".join(
        f"<tr><td class='mono'>{k.replace('|',' vs ')}</td>"
        f"<td>{v['ink_diff']:.4f}</td><td>{v['hist_dist']:.4f}</td>"
        f"<td>{v['page_diff']:.4f}</td><td>{v['ink_area']*100:.1f}%</td></tr>"
        for k, v in sorted(d["pairwise_page_diff"].items(), key=lambda kv: kv[1]["ink_diff"]))
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>稳稿 PPT · 6 主题 6 风格（真实渲染对照）</title>
<style>
:root{{--ink:#1a1a1a;--muted:#5a5a5a;--line:#e2e2e2;--bg:#faf8f5;--surface:#fff}}
body{{margin:0;background:var(--bg);color:var(--ink);
 font:15px/1.65 -apple-system,"PingFang SC","Helvetica Neue",Arial,sans-serif}}
header{{padding:28px 32px 8px}} h1{{margin:0 0 6px;font-size:22px}}
.meta{{color:var(--muted);font-size:13px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:18px;padding:18px 32px 8px}}
.card{{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:16px}}
.card h3{{margin:0;font-size:17px}} .card h3 small{{color:var(--muted);font-weight:400;font-size:12px;margin-left:8px}}
.tag{{color:var(--muted);font-size:13px;margin:6px 0 10px}}
.sw{{display:flex;gap:6px;margin-bottom:10px}} .sw i{{width:26px;height:12px;border-radius:3px;border:1px solid rgba(0,0,0,.08)}}
.shot{{display:grid;gap:6px}} .shot img{{width:100%;border:1px solid var(--line);border-radius:6px;display:block}}
dl{{display:grid;grid-template-columns:56px 1fr;gap:2px 8px;font-size:12px;color:var(--muted);margin:10px 0 0}}
dd{{margin:0}} .mono{{font-family:Menlo,monospace;font-size:11px}}
table{{border-collapse:collapse;margin:8px 32px 32px;font-size:13px;background:var(--surface)}}
th,td{{border:1px solid var(--line);padding:6px 10px;text-align:left}} th{{background:#f2efe9}}
.note{{margin:0 32px 12px;color:var(--muted);font-size:13px}}
</style>
<header>
  <h1>6 个主题 · 6 种风格（同一原稿真实渲染）</h1>
  <div class="meta">原稿 {d['source'].split('/')[-1]}（sha256 {d['source_sha256'][:12]}…） ·
    渲染器 {d['renderer']} · dpi {d['dpi']} ·
    最接近的一对（按内容像素差）：{d['weakest_pair']['pair'].replace('|',' vs ') if d['weakest_pair'] else '—'}
    （ink_diff {d['weakest_pair']['ink_diff'] if d['weakest_pair'] else '—'}）</div>
</header>
<p class="note">{d['scope']}</p>
<section class="grid">{''.join(cards)}</section>
<h2 style="padding:0 32px;font-size:16px">两两页面对比（15 页平均）</h2>
<p class="note">ink_diff = 只在内容像素上算的平均差（不被留白稀释，才是"风格差"）；
hist_dist = 4×4×4 色彩直方图距离（配色构成）；
page_diff = 整页平均差（按面积加权，浅色主题之间会偏小）；
内容占比 = 两页合并后的内容像素比例，用来判断这个 ink_diff 有多少样本支撑。</p>
<table><thead><tr><th>主题对</th><th>ink_diff</th><th>hist_dist</th><th>page_diff</th><th>内容占比</th></tr></thead>
<tbody>{rows}</tbody></table>
</html>"""


if __name__ == "__main__":
    raise SystemExit(main())
