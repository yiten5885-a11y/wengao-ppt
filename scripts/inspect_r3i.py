"""取证 8：扫 slideMaster / slideLayout 的 lstStyle effectLst 与深色定义。只读。"""
from pptx import Presentation
from lxml import etree

PPTX = ("/Users/huangyingteng/WorkBuddy/2026-10-12-00-54-41/wengao-ppt/build/"
        "wengao-visual-v13-material-fix/reports/gate-13p-20261021/v14-readable-13p-dedicated.pptx")
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
prs = Presentation(PPTX)

for mi, master in enumerate(prs.slide_masters):
    xml = etree.tostring(master.element).decode()
    print(f"== master {mi}: effectLst x{xml.count('effectLst')}, "
          f"outerShdw x{xml.count('outerShdw')}, bg x{xml.count('<p:bg>')}")
    for e in master.element.findall(f".//{A}effectLst"):
        parent = e.getparent().tag.split('}')[-1]
        gp = e.getparent().getparent()
        gptag = gp.tag.split('}')[-1]
        sz = gp.get("sz") if gptag in ("defRPr", "rPr") else "?"
        kids = [k.tag.split('}')[-1] for k in e]
        print(f"   effectLst under {parent}<{gptag} sz={sz}>: {kids}")
    for lay in master.slide_layouts:
        xml2 = etree.tostring(lay.element).decode()
        if "effectLst" in xml2:
            print(f"   layout {lay.name}: effectLst x{xml2.count('effectLst')}")
            for e in lay.element.findall(f".//{A}effectLst"):
                parent = e.getparent().tag.split('}')[-1]
                gp = e.getparent().getparent()
                kids = [k.tag.split('}')[-1] for k in e]
                print(f"     effectLst under {parent}: {kids}")
