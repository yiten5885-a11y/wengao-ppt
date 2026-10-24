"""取证 9：解压 PDF 页内容流，找文字绘制指令（双画/描边/颜色）。只读。"""
import re
import zlib

PDF = ("/Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/"
       "wengao-visual-v13-material-fix/reports/gate-13p-20261021/render/v14-readable-13p-dedicated.pdf")
raw = open(PDF, "rb").read()
print("pdf bytes:", len(raw))

# 找到所有 stream 对象并解压，检索含 Tj/TJ 且颜色为 1A1C20 附近有其他颜色的段
streams = []
for m in re.finditer(rb"stream\r?\n", raw):
    start = m.end()
    end = raw.find(b"endstream", start)
    if end < 0:
        continue
    chunk = raw[start:end]
    try:
        dec = zlib.decompress(chunk)
        streams.append(dec)
    except Exception:
        pass
print("decompressed streams:", len(streams))

hit = 0
for s in streams:
    if b"Tj" not in s and b"TJ" not in s:
        continue
    txt = s.decode("latin-1", errors="replace")
    # 文本绘制段：取含 rg + BT..ET 的窗口
    for m2 in re.finditer(r"([\d.]+ [\d.]+ [\d.]+ rg)\s*(?:BT|/)", txt):
        col = m2.group(1)
        if hit < 40:
            pass
    # 统计颜色 ops
    cols = re.findall(r"([\d.]+) ([\d.]+) ([\d.]+) (rg|RG)", txt)
    from collections import Counter
    cc = Counter(cols)
    if any(abs(float(c[0]) - 0.102) < 0.02 and abs(float(c[1]) - 0.11) < 0.02 for c in cc):
        print("== stream with ink-color text ops:", len(txt))
        print("   colors:", cc.most_common(8))
        # 找 Tr（渲染模式）
        trs = re.findall(r"(\d+) Tr", txt)
        print("   Tr modes:", Counter(trs))
        # 找 w（线宽）+ Tr 组合
        wl = re.findall(r"([\d.]+) w\s+(\d+) Tr", txt)
        print("   width+Tr pairs:", wl[:10])
        hit += 1
        if hit >= 6:
            break
