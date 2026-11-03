"""OOXML 结构门禁（scripts/ooxml_conform.py）的正例与反例。

正例：python-pptx 生成的真实文件必须 PASS——否则门禁只会制造噪音。
反例：逐类构造真实损坏（缺部件、悬空 Override、重复 shape id、断关系目标），
断言门禁精确命中对应代码。反例都用**改写 ZIP** 的方式构造，不 mock 检查逻辑。
"""
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ooxml_conform as oc  # noqa: E402


def build_pptx(path: Path, slides: int = 3) -> Path:
    from pptx import Presentation
    from pptx.util import Inches
    prs = Presentation()
    prs.slide_width, prs.slide_height = 12191695, 6858000
    for i in range(slides):
        s = prs.slides.add_slide(prs.slide_layouts[6])
        tb = s.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1))
        tb.text_frame.text = f"第 {i + 1} 页：稳稿结构门禁测试"
    prs.save(str(path))
    return path


def rewrite(src: Path, dst: Path, mutate) -> Path:
    """按 mutate(name, data)->bytes|None 重写包（None 表示删除该部件）。"""
    with zipfile.ZipFile(src) as zin:
        items = [(i, zin.read(i.filename)) for i in zin.infolist()]
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for info, data in items:
            new = mutate(info.filename, data)
            if new is None:
                continue
            zout.writestr(info.filename, new)
    return dst


class PositiveCase(unittest.TestCase):
    def test_generated_pptx_passes_every_check(self):
        tmp = Path(tempfile.mkdtemp(prefix="ooxml-pos-"))
        try:
            p = build_pptx(tmp / "ok.pptx")
            rep = oc.Checker(p).run()
            self.assertEqual(rep["verdict"], "PASS", rep)
            codes = {c["code"] for c in rep["checks"]}
            self.assertEqual(codes, {"C1_ZIP_READABLE", "C2_CONTENT_TYPES",
                                     "C3_RELATIONSHIPS", "C4_SLIDE_LIST",
                                     "C5_SHAPE_IDS", "C6_LAYOUT_MASTER_CHAIN",
                                     "C7_NO_LOCAL_FILE_URIS"})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class NegativeCases(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ooxml-neg-"))
        self.src = build_pptx(self.tmp / "base.pptx")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _failing(self, dst_name):
        rep = oc.Checker(self.tmp / dst_name).run()
        return rep, {c["code"]: c for c in rep["checks"] if not c["ok"]}

    def test_missing_slide_part_hits_c2_c3_c4(self):
        def mutate(name, data):
            return None if name == "ppt/slides/slide1.xml" else data
        rewrite(self.src, self.tmp / "miss.pptx", mutate)
        rep, bad = self._failing("miss.pptx")
        self.assertEqual(rep["verdict"], "FAIL")
        self.assertIn("C2_CONTENT_TYPES", bad)      # Override 悬空
        self.assertIn("C3_RELATIONSHIPS", bad)      # 关系目标不存在
        self.assertIn("C4_SLIDE_LIST", bad)         # sldIdLst 与实际页数不符

    def test_duplicate_shape_id_hits_c5(self):
        def mutate(name, data):
            if name != "ppt/slides/slide2.xml":
                return data
            text = data.decode("utf-8")
            # 把第二个 cNvPr 的 id 改成与第一个相同
            first = text.find('id="')
            assert first > 0
            start = first + len('id="')
            end = text.find('"', start)
            first_id = text[start:end]
            second = text.find('id="', end)
            s2 = second + len('id="')
            e2 = text.find('"', s2)
            text = text[:s2] + first_id + text[e2:]
            return text.encode("utf-8")
        rewrite(self.src, self.tmp / "dup.pptx", mutate)
        rep, bad = self._failing("dup.pptx")
        self.assertIn("C5_SHAPE_IDS", bad, rep)

    def test_dangling_relationship_target_hits_c3(self):
        def mutate(name, data):
            if name != "ppt/slides/_rels/slide1.xml.rels":
                return data
            text = data.decode("utf-8")
            idx = text.find('Target="')
            assert idx > 0
            s = idx + len('Target="')
            e = text.find('"', s)
            return (text[:s] + "../slideLayouts/does-not-exist.xml"
                    + text[e:]).encode("utf-8")
        rewrite(self.src, self.tmp / "dangling.pptx", mutate)
        rep, bad = self._failing("dangling.pptx")
        self.assertIn("C3_RELATIONSHIPS", bad, rep)

    def test_local_file_uri_is_reported(self):
        def mutate(name, data):
            if name != "ppt/slides/_rels/slide1.xml.rels":
                return data
            text = data.decode("utf-8")
            return text.replace(
                "</Relationships>",
                '<Relationship Id="rIdLocal" Type="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships/image" '
                'Target="file:///Users/someone/private.png" TargetMode="External"/>'
                "</Relationships>").encode("utf-8")
        rewrite(self.src, self.tmp / "localuri.pptx", mutate)
        rep, bad = self._failing("localuri.pptx")
        self.assertIn("C3_RELATIONSHIPS", bad, rep)   # 关系层直接拒绝
        self.assertIn("C7_NO_LOCAL_FILE_URIS", bad, rep)


class ExtensionHelperTests(unittest.TestCase):
    def test_dotfile_parts_keep_their_extension(self):
        # 回归：splitext 会把 `_rels/.rels` 当无扩展名，误报内容类型缺失
        self.assertEqual(oc._ext("_rels/.rels"), "rels")
        self.assertEqual(oc._ext("ppt/slides/slide1.xml"), "xml")
        self.assertEqual(oc._ext("ppt/media/image1.PNG"), "png")
        self.assertEqual(oc._ext("ppt/presentation.xml"), "xml")


if __name__ == "__main__":
    unittest.main()
