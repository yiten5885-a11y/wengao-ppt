#!/usr/bin/env python3
"""P0 诊断：从 exact v12 生成单变量诊断副本。

每份副本只改一个变量（控制变量法），不动其他任何字节：
  D0  对照（与 exact 完全一致，仅重打包）
  D1  仅 lang: rPr lang="en-US" → "zh-CN"（全部 slide run）
  D2  仅 charset: 删除 a:latin/a:ea/a:cs 上的 charset/pitchFamily 属性
  D3  仅 cs: a:cs typeface "Microsoft YaHei" → "Hiragino Sans GB"
  D4  仅 latin: a:latin typeface "Avenir Next" → "Hiragino Sans GB"
  D5  仅 latin: a:latin typeface "Avenir Next" → "Songti SC"（选槽探针：
      ea 保持 Hiragino Sans GB，两种字体可区分 → PDF 逐字符字体名
      直接显示 LibreOffice 给 CJK 字符选了哪个槽）
"""
import re, shutil, zipfile, sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[3]
SRC = BASE / "reports/p0-native-glyph-20261020/evidence/exact_v12.pptx"
OUT = BASE / "reports/p0-native-glyph-20261020/diag"
OUT.mkdir(parents=True, exist_ok=True)

NS = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'


def read_pptx(path):
    z = zipfile.ZipFile(path)
    return {n: z.read(n) for n in z.namelist()}, z.namelist()


def write_pptx(members, order, out):
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for n in order:
            z.writestr(n, members[n])


def slide_parts(order):
    return [n for n in order if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]


def transform_lang(xml: bytes) -> bytes:
    return xml.replace(b'lang="en-US"', b'lang="zh-CN"')


def transform_charset(xml: bytes) -> bytes:
    # 删除 a:latin/a:ea/a:cs 元素上的 charset 与 pitchFamily 属性
    def strip_attrs(m):
        tag = m.group(0)
        tag = re.sub(rb'\s+charset="-?\d+"', b"", tag)
        tag = re.sub(rb'\s+pitchFamily="-?\d+"', b"", tag)
        return tag
    return re.sub(rb"<a:(latin|ea|cs)\b[^>]*/>", strip_attrs, xml)


def transform_cs(xml: bytes) -> bytes:
    return xml.replace(b'<a:cs typeface="Microsoft YaHei"',
                       b'<a:cs typeface="Hiragino Sans GB"')


def transform_latin(xml: bytes, new: bytes) -> bytes:
    return xml.replace(b'<a:latin typeface="Avenir Next"',
                       b'<a:latin typeface="' + new + b'"')


VARIANTS = {
    "D0-exact": lambda x: x,
    "D1-lang-zhCN": transform_lang,
    "D2-no-charset": transform_charset,
    "D3-cs-hiragino": transform_cs,
    "D4-latin-hiragino": lambda x: transform_latin(x, b"Hiragino Sans GB"),
    "D5-latin-songti": lambda x: transform_latin(x, b"Songti SC"),
}


def main():
    members, order = read_pptx(SRC)
    slides = slide_parts(order)
    print(f"slides: {len(slides)}")
    for name, fn in VARIANTS.items():
        new_members = dict(members)
        for s in slides:
            new_members[s] = fn(members[s])
        out = OUT / f"{name}.pptx"
        write_pptx(new_members, order, out)
        changed = sum(1 for s in slides if new_members[s] != members[s])
        print(f"{name}: 重打包完成（改动 slide 数 {changed}）→ {out.name}")


if __name__ == "__main__":
    main()
