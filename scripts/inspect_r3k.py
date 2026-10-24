"""取证 11：dump Tr 2 前后 800 字符的原始内容流窗口。只读。"""
import re
import zlib

PDF = ("/Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/"
       "wengao-visual-v13-material-fix/reports/gate-13p-20261021/render/v14-readable-13p-dedicated.pdf")
raw = open(PDF, "rb").read()
streams = []
for m in re.finditer(rb"stream\r?\n", raw):
    start = m.end()
    end = raw.find(b"endstream", start)
    if end < 0:
        continue
    try:
        streams.append(zlib.decompress(raw[start:end]))
    except Exception:
        pass

shown = 0
for s in streams:
    if b"2 Tr" not in s:
        continue
    txt = s.decode("latin-1", errors="replace")
    for m in re.finditer(r"2 Tr", txt):
        a = max(m.start() - 700, 0)
        b = min(m.end() + 500, len(txt))
        win = txt[a:b]
        print("=" * 70)
        print(win)
        shown += 1
        break
    shown += 1
    if shown >= 3:
        break
