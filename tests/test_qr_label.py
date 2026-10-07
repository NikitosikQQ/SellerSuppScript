"""Наклейка пилы: размер, номер текстом, прежняя наклейка 26.10.5, склейка и подгонка 58×40."""
import os
import shutil
import tempfile
import unittest

import fitz

from qr_label import MM, build_label_pdf, merge_pdfs
from seller_supp_api import _normalize_to_label


class QrLabelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qr_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.tmp, name)

    def page_mm(self, pdf, index=0):
        with fitz.open(pdf) as doc:
            r = doc[index].rect
            return round(r.width / MM, 1), round(r.height / MM, 1)

    def test_label_with_text_is_58x40_and_contains_order_number(self):
        out = self.path("a.pdf")
        build_label_pdf("0112970827-0530-1", out, with_text=True)
        self.assertEqual(self.page_mm(out), (58.0, 40.0))
        with fitz.open(out) as doc:
            self.assertIn("0112970827-0530-1", doc[0].get_text())
            self.assertEqual(len(doc[0].get_images()), 1, "на наклейке один QR")

    def test_long_number_shrinks_font_but_stays_on_label(self):
        out = self.path("long.pdf")
        number = "1234567890123456789012345678"
        build_label_pdf(number, out, with_text=True)
        with fitz.open(out) as doc:
            page = doc[0]
            words = page.get_text("words")
            self.assertTrue(words)
            x0 = min(w[0] for w in words)
            x1 = max(w[2] for w in words)
            self.assertGreaterEqual(x0, 0)
            self.assertLessEqual(x1, page.rect.width)

    def test_label_without_text_is_old_38mm_qr_only(self):
        out = self.path("old.pdf")
        build_label_pdf("58778784826", out, with_text=False)
        self.assertEqual(self.page_mm(out), (38.0, 38.0))
        with fitz.open(out) as doc:
            self.assertEqual(doc[0].get_text().strip(), "", "прежняя наклейка — только QR")

    def test_merge_keeps_page_order_and_count(self):
        paths = []
        for number in ("A-1", "B-2", "C-3"):
            p = self.path(f"{number}.pdf")
            build_label_pdf(number, p, with_text=True)
            paths.append(p)
        merged = self.path("merged.pdf")
        merge_pdfs(paths, merged)
        with fitz.open(merged) as doc:
            self.assertEqual(doc.page_count, 3)
            texts = [doc[i].get_text() for i in range(3)]
        for i, number in enumerate(("A-1", "B-2", "C-3")):
            self.assertIn(number, texts[i])

    def test_normalize_merged_pdf_gives_58x40_pages(self):
        paths = []
        for number, with_text in (("A-1", True), ("B-2", False)):
            p = self.path(f"{number}.pdf")
            build_label_pdf(number, p, with_text=with_text)
            paths.append(p)
        merged = self.path("merged.pdf")
        merge_pdfs(paths, merged)
        normalized = _normalize_to_label(merged)
        with fitz.open(normalized) as doc:
            self.assertEqual(doc.page_count, 2)
            for i in range(2):
                r = doc[i].rect
                self.assertEqual((round(r.width / MM, 1), round(r.height / MM, 1)), (58.0, 40.0))


if __name__ == "__main__":
    unittest.main()
