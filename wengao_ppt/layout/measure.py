"""W2　文本测量：宽度/行数/高度估算，供版式引擎做布局预判。

诚实边界：这里是**近似测量**（按文字类别的平均字宽），误差约 ±10%，
只用于布局决策（放不放得下、要不要两栏、要不要分页）。硬门禁的最终
"是否溢出/是否遮挡"以 LibreOffice 真实渲染复核（check_layout + 渲染证据）为准，
本模块的结论不冒充渲染结果。

字号全部来自 tokens.TYPE_ROLES（硬门禁档），测量**不改字号**：
装不下就如实报溢出，由引擎换版式或由规划器分页，绝不缩字硬塞。
"""
from __future__ import annotations

import unicodedata

from . import tokens

EMU_PER_PT = 12700  # 1pt = 12700 EMU


def _char_class(ch: str) -> str:
    o = ord(ch)
    if unicodedata.east_asian_width(ch) in ("F", "W"):
        return "wide"
    if 0x0600 <= o <= 0x06FF:
        return "arabic"
    if 0x0900 <= o <= 0x097F:
        return "devanagari"
    if 0x0400 <= o <= 0x04FF:
        return "cyrillic"
    if ch == " ":
        return "space"
    if ch.isdigit():
        return "digit"
    if ch.isalpha():
        return "alpha"
    return "punct"


#: 各类别平均字宽（em，相对字号）。取保守偏大值，避免低估高度。
_AVG_EM = {
    "wide": 1.0, "arabic": 0.52, "devanagari": 0.62, "cyrillic": 0.58,
    "space": 0.30, "digit": 0.56, "alpha": 0.53, "punct": 0.34,
}

#: 数字后可粘连的中文量词/单位（硬门禁 seed_protected_tokens 里的 "2500亿"/"36个月"）
_CJK_UNITS = "亿万元个家人员倍次项条台套份期季度天日点"


def text_width_emu(text: str, pt: float) -> int:
    """单行不换行时的估算宽度（EMU）。"""
    total_em = sum(_AVG_EM[_char_class(c)] for c in str(text or ""))
    return int(round(total_em * pt * EMU_PER_PT))


def wrap_lines(text: str, width_emu: int, pt: float) -> list[str]:
    """贪心换行的估算行拆分。断行只在可断处（空格 / 任意 CJK 字符间）；
    硬门禁禁止把"数字+单位"或拉丁词拆开，这里对数字token做保护。"""
    text = str(text or "")
    if not text:
        return [""]
    lines, cur, cur_w = [], [], 0
    i = 0
    n = len(text)
    iteration_guard = 0
    max_iterations = n * 4 + 100
    while i < n:
        iteration_guard += 1
        if iteration_guard > max_iterations:
            # 双重保险：绝不允许死循环拖垮管线与门禁
            if cur:
                lines.append("".join(cur))
            rest = text[i:]
            if rest:
                lines.append(rest)
            return lines or [""]

        # 数字+单位 token 保护：连续数字/小数点/逗号与其后紧跟的 %、拉丁单位、
        # 中文量词（亿/万/个月…）取为一个 token
        j = i
        if text[i].isdigit() or (text[i] in "+-" and i + 1 < n and text[i + 1].isdigit()):
            if text[i] in "+-":
                j += 1                     # 符号位先吃掉，再吃数字主体
            while j < n and (text[j].isdigit() or text[j] in ".,"):
                j += 1
            while j < n and (text[j] == "%" or (text[j].isalpha() and text[j].isascii())):
                j += 1
            k = j
            while k < n and text[k] in _CJK_UNITS and (k - j) < 4:
                k += 1
            if k > j:
                j = k
            # 保证至少前进 1 个字符
            j = max(j, i + 1)
            token = text[i:j]
        else:
            token = text[i]
            j = i + 1
        if not token:                      # 防御：绝不允许空 token
            token, j = text[i], i + 1
        # 严格单调前进量保证
        if j <= i:
            j = i + 1
            token = text[i:j]

        w = text_width_emu(token, pt)
        if cur and cur_w + w > width_emu:
            # 换行：去掉行尾悬挂空格
            lines.append("".join(cur).rstrip() if "".join(cur).endswith(" ")
                         else "".join(cur))
            cur, cur_w = [], 0
            if token == " ":
                i = j
                continue
        cur.append(token)
        cur_w += w
        i = j
    if cur:
        lines.append("".join(cur))
    return lines or [""]


def block_height_emu(text: str, width_emu: int, pt: float,
                     line_height: float = 1.4, space_after_pt: float = 0.0,
                     para_spacing: bool = True) -> int:
    """文本块在给定宽度下的估算高度（含段间距）。"""
    paras = [p for p in str(text or "").split("\n")]
    total = 0
    for k, para in enumerate(paras):
        lines = wrap_lines(para, width_emu, pt)
        total += len(lines) * int(round(pt * line_height * EMU_PER_PT))
        if para_spacing and k < len(paras) - 1:
            total += int(round(space_after_pt * EMU_PER_PT))
    return total


def fits(text: str, width_emu: int, height_emu: int, pt: float,
         line_height: float = 1.4) -> dict:
    """文本是否装得下。返回行数/需要高度/结论；**不做任何缩字**。"""
    need = block_height_emu(text, width_emu, pt, line_height)
    lines = wrap_lines(text, width_emu, pt)
    return {
        "fits": need <= height_emu,
        "lines": len(lines),
        "needed_h_emu": need,
        "box_h_emu": height_emu,
        "pt": pt,
        "margin_h_emu": height_emu - need,
    }


def measure_blocks(texts: list[str], width_emu: int, pt: float,
                   line_height: float = 1.4, space_after_pt: float = 6.0) -> dict:
    """一组块的总高度与逐块明细（引擎分栏决策用）。"""
    details, total = [], 0
    for t in texts:
        h = block_height_emu(t, width_emu, pt, line_height, space_after_pt)
        details.append({"text_head": str(t)[:24], "lines": len(wrap_lines(t, width_emu, pt)),
                        "h_emu": h})
        total += h
    return {"blocks": len(texts), "total_h_emu": total, "details": details}
