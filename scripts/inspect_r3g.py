"""取证 6：保存问题字符 crop 放大图，直视中灰色来源。只读。"""
from PIL import Image
import numpy as np

OUT = ("/Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/"
       "wengao-visual-v13-material-fix/reports/gate-13p-20261021/render")
# (页, x0,y0,x1,y1 英寸, 名称)
crops = [
    (7, 12.45, 3.70, 13.00, 4.20, "P7_liang"),
    (2, 5.75, 6.60, 6.15, 6.95, "P2_ju"),
    (12, 0.60, 0.15, 3.20, 1.20, "P12_title"),
    (7, 3.90, 4.95, 4.25, 5.15, "P7_comma"),
]
for pg, x0, y0, x1, y1, name in crops:
    img = Image.open(f"{OUT}/full-P{pg:02d}.png").convert("RGB")
    sx = img.size[0] / 13.333
    sy = img.size[1] / 7.5
    c = img.crop((int(x0 * sx), int(y0 * sy), int(x1 * sx), int(y1 * sy)))
    big = c.resize((c.size[0] * 6, c.size[1] * 6), Image.NEAREST)
    big.save(f"/tmp/crop_{name}.png")
    arr = np.asarray(c, dtype=np.uint8).reshape(-1, 3)
    # 输出中灰像素(120-210)占比与最常见中灰值
    g = arr.mean(axis=1)
    mid = arr[(g > 120) & (g < 210)]
    if len(mid):
        vals, counts = np.unique(mid.reshape(-1, 3), axis=0, return_counts=True)
        top = sorted(zip(counts, vals.tolist()), reverse=True)[:4]
        print(f"{name}: mid_px={len(mid)}/{len(arr)} top={top}")
    else:
        print(f"{name}: no mid px")
print("saved to /tmp/crop_*.png")
