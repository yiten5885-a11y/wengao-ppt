"""渲染门禁：真实渲染（LibreOffice）+ PDF 页数结构化解析 + 逐页 PNG。

- 真实渲染缺失/失败 → RENDER.NOT_AVAILABLE / RENDER.EXECUTION_FAILED，不放行。
- 近似预览（render_lite）只能作为调试辅助，明确标注，绝不算真实渲染通过。
- PDF 页数以成熟解析器读取实际文件所得的页对象数量为准（pypdf.PdfReader），
  不使用 PDF 原始字节里的 /Type /Page 命中数。
- 逐页 PNG 优先 pdftoppm；缺失时用 pypdfium2（自带 PDFium，无系统依赖）对
  **真实 PDF** 逐页栅格化。两者栅格化的都是 LibreOffice 实际排版的 PDF，
  不是程序近似预览；渲染器身份与版本一律写入证据。
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from . import fontenv
from .core import sha256_file

# macOS 上 LibreOffice 不在 PATH 里，需显式探测应用包内的 soffice。
# 用户级路径（2026-11-05）：libreoffice_setup 默认装 /Applications，但在受管
# 沙箱环境里 /Applications 是覆盖层视图——安装后对后续进程时有时无（实测
# 12:16 装好、12:17 能渲染、12:33 起宿主与沙箱都看不到）。把安装目标落到
# ~/.wengao_ppt/libreoffice/（真实用户目录，不受视图漂移影响），渲染器因此
# 稳定可用；/Applications 仍保留在搜索首位，标准安装照常生效。
_MAC_BUNDLES = [
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    str(Path.home() / "Applications" / "LibreOffice.app" / "Contents" / "MacOS" / "soffice"),
    str(Path.home() / ".wengao_ppt" / "libreoffice" / "LibreOffice.app" / "Contents" / "MacOS" / "soffice"),
]

#: `render_pages` 的缩略图档 DPI（列表预览用；细看用调用方传入的大图档 dpi）
THUMB_DPI = 48


def find_soffice():
    """定位真实 LibreOffice 可执行文件；不修改系统 PATH，不做符号链接。"""
    for c in ("soffice", "libreoffice"):
        p = shutil.which(c)
        if p:
            return p, "PATH"
    for p in _MAC_BUNDLES:
        if Path(p).exists():
            return p, "mac_bundle"
    return None, None


def _fontconfig_env(data_root=None):
    """兼容旧调用点的薄包装：真正的构造逻辑只在 `wengao_ppt.fontenv`。

    返回 `(env, conf_path)`。`fontenv.build_env` 还给出 `ok` 与指纹；
    需要判"包内资源是否齐全"的调用方必须用 `fontenv.build_env` 的 record，
    不能只看这里有没有路径（路径存在不等于解析成功）。
    """
    env, rec = fontenv.build_env(data_root)
    return env, rec.get("effective_conf")


def pdf_font_families(pdf_path):
    """从真实 PDF 的字体资源里取**实际解析到**的字体名（不带子集前缀）。

    这是"字体解析结果"的证据来源：名称不是覆盖率证明（见 glyphcheck），
    但它能证明"本次渲染用的是哪一套字体"，可用于环境指纹与 A/B 对比。
    """
    try:
        from pypdf import PdfReader
    except Exception:  # noqa: BLE001
        return [], "pypdf 未安装"
    names = set()
    try:
        reader = PdfReader(str(pdf_path))
        for page in reader.pages:
            res = page.get("/Resources")
            if not res:
                continue
            fonts = res.get_object().get("/Font")
            if not fonts:
                continue
            for _k, ref in fonts.get_object().items():
                f = ref.get_object()
                base = str(f.get("/BaseFont") or "")
                if "+" in base:
                    base = base.split("+", 1)[1]
                if base:
                    names.add(base)
    except Exception as e:  # noqa: BLE001
        return sorted(names), f"{type(e).__name__}: {e}"
    return sorted(names), None



def soffice_version(soffice):
    try:
        r = subprocess.run([soffice, "--version"], capture_output=True, text=True, timeout=60)
        return (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr).strip() else None
    except Exception:  # noqa: BLE001
        return None


def real_render_available() -> bool:
    return find_soffice()[0] is not None


def _pdfium_version():
    """记录栅格化引擎的身份与版本；拿不到就如实记 None，不编造。"""
    try:
        import pypdfium2 as p
        from importlib.metadata import version
        pv = version("pypdfium2")
        info = getattr(p, "PDFIUM_INFO", None)
        fv = None
        if info is not None:
            fv = getattr(info, "version", None) or getattr(info, "build", None)
        return f"pypdfium2 {pv} / pdfium {fv}" if fv else f"pypdfium2 {pv}"
    except Exception:  # noqa: BLE001
        return None


def _render_png_pdfium(pdf: Path, png_dir: Path, dpi=110):
    """用 PDFium 把真实 PDF 逐页栅格化为 PNG。失败即失败，不伪造图片。

    返回 (产出页数, PDF 实际页数)；调用方必须核对两者一致。
    """
    import pypdfium2 as pdfium
    _ensure_dir(png_dir)
    doc = pdfium.PdfDocument(str(pdf))
    n = 0
    scale = dpi / 72.0
    total = len(doc)
    for i in range(total):
        page = doc[i]
        img = page.render(scale=scale).to_pil()
        out = png_dir / f"page-{i + 1:02d}.png"
        img.save(str(out))
        n += 1
    doc.close()
    return n, total


def png_bindings(pdf, png_dir, rasterizer, dpi):
    """把每张 PNG 绑定到：本次 PDF、页码、实际栅格化程序与版本、DPI、PNG 自身哈希。

    预览来源保护（规格 §8）：没有这些绑定，"上游近似 PNG 顶替真实预览"与
    "PDF 换了但 PNG 没换"都无法被发现。绑定信息随渲染证据一起落盘。
    """
    bindings = []
    if not pdf or not Path(pdf).exists() or not png_dir or not Path(png_dir).exists():
        return bindings
    pdf_hash = sha256_file(pdf)
    from PIL import Image
    for png in sorted(Path(png_dir).glob("page*.png")):
        try:
            with Image.open(png) as im:
                w, h = im.size
        except Exception as e:  # noqa: BLE001
            bindings.append({"file": str(png), "error": f"无法读取尺寸：{e}",
                             "pdf_sha256": pdf_hash})
            continue
        try:
            page_no = int(png.stem.split("-")[-1])
        except ValueError:
            page_no = None
        bindings.append({
            "file": str(png), "page_no": page_no,
            "pdf_path": str(pdf), "pdf_sha256": pdf_hash,
            "rasterizer": rasterizer, "dpi": dpi,
            "png_sha256": sha256_file(png), "width": w, "height": h,
        })
    return bindings


def _ensure_dir(p):
    """幂等建目录：重复验收时目录已存在必须继续，不能报错中断。"""
    p = Path(p)
    if not p.exists():
        p.mkdir(parents=True, exist_ok=True)
    return p


def _kill_process_group(proc):
    """杀掉整个进程组，而不只是直接子进程。

    渲染器（LibreOffice）会派生自己的子进程；只 kill 直接子进程会让孙进程
    变成孤儿继续跑，取消之后还在写文件，污染下一次 attempt。
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _run_soffice(cmd, timeout, env, pid_file=None):
    """启动渲染子进程并记录其 PID；超时则连同进程组一起回收。"""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env, start_new_session=True)
    if pid_file:
        try:
            Path(pid_file).write_text(str(proc.pid), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    try:
        out, err = proc.communicate(timeout=max(1.0, float(timeout)))
        return {"exit_code": proc.returncode, "stdout": out or "",
                "stderr": err or "", "timed_out": False, "pid": proc.pid}
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        try:
            out, err = proc.communicate(timeout=10)
        except Exception:  # noqa: BLE001
            out, err = "", ""
        return {"exit_code": "timeout", "stdout": out or "",
                "stderr": err or "", "timed_out": True, "pid": proc.pid}


def render_real(pptx_path, outdir, dpi=110, timeout=600, cancel_check=None,
                pid_file=None, data_root=None):
    """真实渲染：PPTX → PDF（LibreOffice），再逐页转 PNG。返回结果字典。

    timeout 是**总预算**：重试不得把总耗时翻倍。每次尝试的 subprocess 超时
    取"剩余预算"，而不是重新给一份完整预算。

    cancel_check 为可调用对象，返回 True 表示任务已被取消/已过期，此时不再
    启动下一次尝试。

    pid_file：把渲染子进程的 PID 写盘，使 cancel 能连带回收渲染进程组——
    只终止 worker 会让 LibreOffice 变成孤儿继续写文件，污染下一次 attempt。

    故障注入（如实标注，不冒充真实渲染结果）：
        WENGAO_LO_FAULT_FIRST=1  首次尝试用一个立刻失败的假 soffice，
                                 用于验证"重试只花剩余预算"与"重试前取消"。
        WENGAO_LO_FAULT_ALL=1    两次尝试都用假 soffice，用于验证
                                 "失败产生的部分产物不进入合格交付"。
    """
    outdir = _ensure_dir(outdir)
    pptx_path = Path(pptx_path)
    deadline = time.time() + float(timeout)
    soffice, src = find_soffice()
    res = {"stage": "render_real", "renderer": "libreoffice",
           "soffice_path": soffice, "soffice_source": src,
           "soffice_version": soffice_version(soffice) if soffice else None,
           "available": soffice is not None,
           "pdf": None, "png_dir": None, "png_count": 0, "png_renderer": None,
           "exit_code": None, "stderr": ""}
    if not soffice:
        res["code"] = "RENDER.NOT_AVAILABLE"
        res["detail"] = "环境中不存在 LibreOffice/soffice；真实渲染无法执行。"
        res["attempt_count"] = 0
        return res

    env, fc_rec = fontenv.build_env(data_root)
    res["font_environment"] = fc_rec
    res["fontconfig_file"] = fc_rec.get("effective_conf")
    # 包内必需资源缺失 → 渲染**不可信**：LibreOffice 仍可能退出 0 并产出全方框 PDF。
    # 这一条必须在启动渲染器之前判，否则"exit 0 + 文本层有字"会把事故原样复制一遍。
    if not fc_rec.get("ok"):
        res["code"] = "RENDER.FONTCONFIG_RESOURCE_MISSING"
        res["detail"] = (fc_rec.get("error") or "字体配置资源不可用")
        res["attempt_count"] = 0
        return res
    # 渲染 profile 隔离：不复用用户既有 LibreOffice 配置，避免"别的会话改过字体缓存"
    # 这类不可复现因素影响验收；同时让故障注入可以拿到真正干净的环境。
    # profile 位置优先级：显式环境变量 → 数据根（安装布局）→ 用户缓存目录。
    # **不再默认落到程序目录**：安装布局下 version 目录应保持只读语义，
    # 渲染缓存写进去会污染构建身份（实测：.lo-profile 使 build_id 漂移）。
    _prof = os.environ.get("WENGAO_LO_PROFILE")
    if not _prof:
        _data = os.environ.get("WENGAO_DATA_ROOT")
        if _data:
            _prof = str(Path(_data) / ".lo-profile")
        else:
            _prof = str(Path.home() / ".cache" / "wengao-ppt" / ".lo-profile")
    shared_profile = Path(_prof)
    _ensure_dir(shared_profile)
    res["lo_profile"] = str(shared_profile)
    pdf = outdir / (pptx_path.stem + ".pdf")

    def _remaining():
        return max(1.0, deadline - time.time())

    def _cancelled():
        try:
            return bool(cancel_check and cancel_check())
        except Exception:  # noqa: BLE001
            return False

    fault_first = os.environ.get("WENGAO_LO_FAULT_FIRST")
    fault_all = os.environ.get("WENGAO_LO_FAULT_ALL")

    def _fake_soffice():
        """受控故障注入用的假渲染器：不等待真实 LibreOffice 自然慢/失败。"""
        p = outdir / "_fake_soffice.sh"
        p.write_text("#!/bin/sh\nsleep 1\nexit 1\n", encoding="utf-8")
        os.chmod(p, 0o755)
        return str(p)

    def _attempt(profile: Path, binary=None):
        budget = _remaining()
        cmd = [binary or soffice, "--headless", "--norestore",
               f"-env:UserInstallation=file://{profile}",
               "--convert-to", "pdf", "--outdir", str(outdir), str(pptx_path)]
        r = _run_soffice(cmd, budget, env, pid_file=pid_file)
        return {"profile": str(profile), "exit_code": r["exit_code"],
                # 本次尝试实际拿到的是**剩余**预算，不是重新给一份完整预算
                "budget": round(budget, 3),
                "pid": r.get("pid"), "timed_out": r.get("timed_out"),
                "stdout_tail": (r["stdout"] or "")[-400:],
                "stderr_tail": (r["stderr"] or "")[-400:],
                "pdf_exists": (outdir / (pptx_path.stem + ".pdf")).exists()}

    attempts = []
    fault_note = None

    def _publish_attempts():
        """统一落盘尝试台账：任何早退路径都不得少记字段，
        否则"到底尝试了几次"在证据里读不出来。"""
        res["attempts"] = attempts
        res["attempt_count"] = len(attempts)
        if fault_note:
            res["fault_injection"] = fault_note
        res["final_attempt_faulted"] = bool(fault_all)
        if attempts:
            res["exit_code"] = attempts[-1]["exit_code"]
            res["stderr"] = attempts[-1].get("stderr_tail", "")
            res["stdout"] = attempts[-1].get("stdout_tail", "")

    try:
        if fault_all:
            fault_note = ("故障注入 WENGAO_LO_FAULT_ALL=1：两次尝试都用假渲染器，"
                          "用于验证失败产生的部分产物不进入合格交付。")
            attempts.append(_attempt(shared_profile, binary=_fake_soffice()))
        elif fault_first:
            fault_note = ("故障注入 WENGAO_LO_FAULT_FIRST=1：首次尝试用假渲染器立即失败，"
                          "第二次改用全新隔离 profile 的真实渲染器。")
            attempts.append(_attempt(shared_profile, binary=_fake_soffice()))
        else:
            attempts.append(_attempt(shared_profile))
        # LibreOffice 会偶发以退出码 1 失败（共享 profile 被占用/首次初始化竞态）。
        # 这时不能用"再跑一次就好"掩盖：两次尝试的退出码与输出都写进证据，
        # 第二次改用全新隔离 profile，以此区分"profile 争用"与"环境真的坏了"。
        # 重试受同一个 deadline 约束，且渲染前先确认任务未被取消/未过期。
        if (attempts[-1]["exit_code"] != 0 or not attempts[-1]["pdf_exists"]):
            if _cancelled():
                res["code"] = "RENDER.EXECUTION_FAILED"
                res["detail"] = "任务在重试渲染前已被取消或已过期；不再启动第二次渲染。"
                _publish_attempts()
                return res
            if time.time() >= deadline:
                res["code"] = "RENDER.EXECUTION_FAILED"
                res["detail"] = f"重试前已耗尽 {timeout}s 总预算；不再启动第二次渲染。"
                _publish_attempts()
                return res
            with tempfile.TemporaryDirectory(prefix="wengao-lo-profile-") as td:
                attempts.append(_attempt(_ensure_dir(Path(td) / "profile")))
    except subprocess.TimeoutExpired:  # 兜底：正常路径已在 _run_soffice 内处理超时
        attempts.append({"profile": str(shared_profile), "exit_code": "timeout"})
        res["code"] = "RENDER.EXECUTION_FAILED"
        res["detail"] = f"真实渲染超过 {timeout}s 总预算（含重试）。"
        _publish_attempts()
        return res

    _publish_attempts()
    produced = outdir / (pptx_path.stem + ".pdf")
    if attempts[-1]["exit_code"] != 0 or not produced.exists():
        res["code"] = "RENDER.EXECUTION_FAILED"
        res["detail"] = ("LibreOffice 退出码非零或未产出 PDF；"
                         f"共尝试 {len(attempts)} 次（含一次全新隔离 profile 重试），"
                         "两次的退出码与输出已记录在 attempts 中。")
        return res
    if len(attempts) > 1:
        res["render_note"] = ("首次渲染失败后改用全新隔离 LibreOffice profile 重试成功；"
                              "首次退出码与输出见 attempts[0]，不因重试而隐瞒。")
    res["pdf"] = str(produced)
    res["pdf_sha256"] = sha256_file(produced)
    # 实际解析到的字体（证据）+ 环境指纹（缓存键）。两者都来自真实产物，
    # 不从主题配置或字体名清单反推。
    fonts, ferr = pdf_font_families(produced)
    res["pdf_fonts"] = fonts
    res["pdf_fonts_error"] = ferr
    res["environment_fingerprint"] = fontenv.fingerprint(
        fc_rec,
        {"version": res.get("soffice_version")},
        fonts)

    # 逐页 PNG：pdftoppm 优先，缺失则用 PDFium 栅格化同一份真实 PDF
    png_dir = _ensure_dir(outdir / "png")
    pdf_pages, perr = pdf_page_count(produced)
    res["pdf_pages_for_png"] = pdf_pages
    pdftoppm = shutil.which("pdftoppm")
    png_error = None
    if pdftoppm:
        try:
            r2 = subprocess.run([pdftoppm, "-png", "-r", str(dpi), str(produced), str(png_dir / "page")],
                                capture_output=True, text=True, timeout=timeout)
            res["png_exit_code"] = r2.returncode
            res["png_renderer"] = "pdftoppm"
            res["png_count"] = len(list(png_dir.glob("page*.png")))
            res["png_dir"] = str(png_dir)
            if r2.returncode != 0:
                png_error = f"pdftoppm 退出码 {r2.returncode}：{(r2.stderr or '')[-200:]}"
        except Exception as e:  # noqa: BLE001
            png_error = f"pdftoppm 执行异常：{type(e).__name__}: {e}"
    else:
        try:
            n, total = _render_png_pdfium(produced, png_dir, dpi=dpi)
            res["png_renderer"] = _pdfium_version()
            res["png_count"] = n
            res["png_dir"] = str(png_dir)
            res["png_detail"] = ("pdftoppm 未安装；改用 PDFium 对 LibreOffice 产出的真实 PDF "
                                 "逐页栅格化，渲染器身份与版本已记录。")
            if n != total:
                png_error = f"PDFium 产出 {n} 页，PDF 实际 {total} 页"
        except Exception as e:  # noqa: BLE001
            png_error = f"逐页 PNG 生成失败（{type(e).__name__}: {e}）"
            res["png_detail"] = f"{png_error}；记为未完成，不用其他方式冒充。"

    # 预览阻断：PDF 渲染成功但逐页 PNG 没出来（或页数不齐）→ 必须阻断。
    # 只看 PDF 退出码会把"没有预览"当成"预览正常"；上游近似 PNG 也不得顶替。
    if res["png_count"] == 0 or (pdf_pages is not None and res["png_count"] != pdf_pages):
        res["code"] = "RENDER.PNG_RASTERIZATION_FAILED"
        res["detail"] = (f"PDF 已产出但逐页预览缺失或不完整（PNG {res['png_count']} 张，"
                         f"PDF {pdf_pages} 页{('；' + png_error) if png_error else ''}）。"
                         "预览缺失必须阻断验收，不得以渲染器退出码或近似图代替。")
        res["png_error"] = png_error
        return res

    res["png_bindings"] = png_bindings(produced, png_dir, res["png_renderer"], dpi)
    res["code"] = "RENDER.OK"
    return res


def cjk_gate_finding(rr, cjk_units_in_input):
    """中文出字门禁：输入有中文、渲染产物文本层没有中文 → 阻断。

    单独抽成函数是为了让这条门禁可以被直接验证——它在真实环境里的触发条件
    （渲染器能跑但字体不可读）难以稳定构造，但判定逻辑必须可测。
    """
    if rr.get("code") not in ("RENDER.OK",):
        return None
    if not cjk_units_in_input:
        return None
    if rr.get("cjk_glyphs_in_render"):
        return None
    return {"code": "RENDER.CJK_GLYPH_MISSING", "severity": "BLOCKING",
            "origin": "render", "slide": None,
            "expected": f"输入含中文单元 {cjk_units_in_input} 个",
            "actual": f"渲染产物文本层中文字符 {rr.get('cjk_glyphs_in_render')}",
            "detail": "真实渲染产物里没有中文；渲染器退出码 0 会掩盖字体不可读这种静默失败。"}


def pdf_cjk_stats(pdf_path):
    """统计真实渲染 PDF 文本层里的中文字符数。

    用于拦住"渲染成功但中文整体没出字"这种静默失败：
    本机曾因缺少 fontconfig 配置，LibreOffice 看不到任何系统字体，
    中文全部消失而渲染器仍退出 0。只看退出码会完全漏掉这种失败。
    """
    try:
        from pypdf import PdfReader
        import re
    except Exception as e:  # noqa: BLE001
        return None, f"依赖缺失: {e}"
    try:
        reader = PdfReader(str(pdf_path))
        txt = "".join((pg.extract_text() or "") for pg in reader.pages)
        cjk = re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", txt)
        return {"pages": len(reader.pages), "text_len": len(txt), "cjk_chars": len(cjk)}, None
    except Exception as e:  # noqa: BLE001
        return None, f"PDF 文本层解析失败: {type(e).__name__}: {e}"


def pdf_page_count(pdf_path):
    """结构化解析 PDF 页数。失败即失败，不回退到猜测计数。"""
    try:
        from pypdf import PdfReader
    except Exception:  # noqa: BLE001
        return None, "pypdf 未安装"
    try:
        reader = PdfReader(str(pdf_path))
        return len(reader.pages), None
    except Exception as e:  # noqa: BLE001
        return None, f"PDF 解析失败: {type(e).__name__}: {e}"


# --------------------------------------------------------------------------
# W2：render_pages —— 版式与成稿（W2）对 render_real 的封装（契约 C6）。
# 提供：PDF、逐页 PNG（预览缩略图与逐页看图共用一套真实渲染产物）、
# 字体报告（PDF 实际解析到的字体 + 中文出字统计 + 页数核对）。
# 保留 render_real 的取消检查与进程清理语义（cancel_check 透传）。
# --------------------------------------------------------------------------
def render_pages(pptx, outdir, *, dpi=110, timeout=600, cancel_check=None,
                 pid_file=None, data_root=None) -> dict:
    """渲染 PPTX → PDF + 逐页 PNG（**缩略图 + 大图两档**）+ 字体报告（契约 C6 签名）。

    两档输出（W2 指令 §4-G）：
    - 大图档：`<outdir>/png/page-XX.png`（dpi，默认 110，细看用）；
    - 缩略图档：`<outdir>/png/thumb/page-XX.png`（THUMB_DPI，列表预览用）。
    两档都栅格化**同一份真实 PDF**，各自记录渲染器身份与文件哈希；
    缩略图失败不影响大图档交付，但会在 fonts_report 里如实记 error。

    返回 {"ok", "code", "pdf", "pngs", "thumbs", "fonts_report", "render"}；
    渲染失败时 ok=False，如实带错误码，不伪造产物。
    """
    rr = render_real(pptx, outdir, dpi=dpi, timeout=timeout,
                     cancel_check=cancel_check, pid_file=pid_file,
                     data_root=data_root)
    pdf = rr.get("pdf")
    pngs = sorted(str(p) for p in Path(rr.get("png_dir") or outdir).glob("page*.png")) \
        if rr.get("png_dir") else []
    fonts_report: dict = {
        "pdf_font_families": rr.get("pdf_fonts"),
        "pdf_fonts_error": rr.get("pdf_fonts_error"),
        "soffice_version": rr.get("soffice_version"),
        "png_renderer": rr.get("png_renderer"),
        "pdf_sha256": rr.get("pdf_sha256"),
    }
    if pdf and Path(pdf).exists():
        stats, err = pdf_cjk_stats(pdf)
        fonts_report["cjk_stats"] = stats
        fonts_report["cjk_stats_error"] = err
        n_pages, perr = pdf_page_count(pdf)
        fonts_report["pdf_page_count"] = n_pages
        fonts_report["pdf_page_count_error"] = perr
    # 缩略图档：同一份 PDF 再栅格化一次（小 dpi）。失败只记 error，
    # 不冒充、也不让大图档已完成的交付作废。
    thumbs: list[str] = []
    if pdf and Path(pdf).exists():
        try:
            thumb_dir = _ensure_dir(Path(outdir) / "png" / "thumb")
            n, total = _render_png_pdfium(Path(pdf), thumb_dir, dpi=THUMB_DPI)
            thumbs = sorted(str(p) for p in thumb_dir.glob("page*.png"))
            fonts_report["thumb"] = {
                "dpi": THUMB_DPI, "renderer": _pdfium_version(),
                "count": n, "pdf_pages": total, "dir": str(thumb_dir),
                "png_sha256": {Path(p).name: sha256_file(p) for p in thumbs},
                "error": None if n == total else f"缩略图 {n} 张 / PDF {total} 页",
            }
        except Exception as e:  # noqa: BLE001
            fonts_report["thumb"] = {"dpi": THUMB_DPI, "count": 0,
                                     "error": f"{type(e).__name__}: {e}"}
    ok = rr.get("code") == "RENDER.OK"
    return {
        "ok": ok, "code": rr.get("code"), "detail": rr.get("detail"),
        "pdf": pdf if ok else None,
        "pngs": pngs if ok else [],
        "thumbs": thumbs if ok else [],
        "png_tiers": {"large": {"dpi": dpi, "count": len(pngs)},
                      "thumb": {"dpi": THUMB_DPI, "count": len(thumbs)}},
        "fonts_report": fonts_report,
        "render": rr,
    }
