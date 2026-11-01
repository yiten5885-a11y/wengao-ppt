"""Regression test for an HG08 ORDINAL_UNBOUND false positive found live
on job-d239567879df, page 2 (a table-of-contents page).

The "is this ordinal badge attached to some real content" check only
looked for a body/label/conclusion shape to the ordinal's *right* on the
same row. A classic right-aligned page-number column — chapter number on
the left ("01"), chapter title next to it, then the page number far right
("03") on the same row, nothing existing further right than it — always
failed: there is no shape further right of the rightmost column. Three of
the six ordinals on this real page (all in the page-number column) were
flagged ORDINAL_UNBOUND even though each sits directly beside its own
chapter title.
"""
import unittest


def _check_ordinal_unbound(shapes, roles):
    # The ordinal-binding loop lives inline inside layout_gate.py's main
    # per-page processing function (not its own callable), so this mirrors
    # that exact rule rather than calling it directly — keep this in sync
    # with the "序号绑定条目" block in scripts/layout_gate.py if that rule
    # changes.
    unbound = []
    for i, s in enumerate(shapes):
        if roles.get(i) != "ordinal":
            continue
        bound = False
        for j, o in enumerate(shapes):
            if j == i or roles.get(j) not in ("body", "label", "conclusion"):
                continue
            if abs(o["top"] - s["top"]) < 1.2:
                bound = True
                break
        if not bound:
            unbound.append(i)
    return unbound


class TestOrdinalBoundBidirectional(unittest.TestCase):

    def _toc_row(self, chapter_no, title, page_no, top):
        return [
            {"text": chapter_no, "left": 4.062, "top": top},
            {"text": title, "left": 4.938, "top": top},
            {"text": page_no, "left": 12.521, "top": top},
        ]

    def test_right_aligned_page_number_column_is_bound(self):
        shapes = (self._toc_row("01", "落差：三组数字", "03", 2.531)
                  + self._toc_row("02", "机制：差距如何被固化", "07", 3.583)
                  + self._toc_row("03", "出路：从补短板到共享", "12", 4.635))
        roles = {i: ("ordinal" if s["text"].strip().isdigit() and i % 3 != 1
                     else "body")
                for i, s in enumerate(shapes)}
        unbound = _check_ordinal_unbound(shapes, roles)
        self.assertEqual(unbound, [])

    def test_truly_orphaned_ordinal_still_flagged(self):
        shapes = [
            {"text": "99", "left": 12.0, "top": 0.2},
            {"text": "unrelated footer", "left": 0.5, "top": 7.0},
        ]
        roles = {0: "ordinal", 1: "body"}
        unbound = _check_ordinal_unbound(shapes, roles)
        self.assertEqual(unbound, [0])


if __name__ == "__main__":
    unittest.main()
