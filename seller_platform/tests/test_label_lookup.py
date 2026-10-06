"""Поиск страницы этикетки: код новой этикетки OZON и прежний поиск по номеру.

Запуск: .venv/Scripts/python.exe -m unittest discover -s tests -t . -v
Прогон на реальном PDF цеха: переменная окружения SS_TEST_PDF=<путь к packages.pdf>.
"""
import os
import unittest

from label_lookup import (
    AMBIGUOUS_CODE, CODE_NOT_FOUND, FOUND_BY_CODE, FOUND_BY_NUMBER, FOUND_BY_PARTIAL,
    FOUND_ONLY_PAGE, NOT_FOUND, build_code_index, find_page_by_number, normalize_code,
    resolve_page, resolve_single_order_page, result_message,
)

# Текстовый слой реальных страниц PDF 05.10.2026 (PyMuPDF): кириллица города не извлекается.
NEW_LABEL_1081 = ["ii5007026 1081", "����-���_60"]
NEW_LABEL_1080 = ["ii5007026 1080"]
OLD_LAYOUT_OZON = ["���������_7", "013582 2844", "-0538-1"]
WB_LABEL = ["WB", "5868998", "0160"]


class BuildCodeIndexTest(unittest.TestCase):

    def test_new_label_line_with_space_is_indexed(self):
        index = build_code_index([NEW_LABEL_1081, NEW_LABEL_1080])

        self.assertEqual(index, {"ii50070261081": [0], "ii50070261080": [1]})

    def test_qr_form_without_space_is_indexed(self):
        self.assertEqual(build_code_index([["ii50070261081"]]), {"ii50070261081": [0]})

    def test_old_layout_and_wb_pages_have_no_codes(self):
        self.assertEqual(build_code_index([OLD_LAYOUT_OZON, WB_LABEL]), {})

    def test_same_code_on_two_pages_is_kept_for_both(self):
        self.assertEqual(build_code_index([NEW_LABEL_1081, NEW_LABEL_1081]), {"ii50070261081": [0, 1]})


class ResolvePageTest(unittest.TestCase):

    def setUp(self):
        self.pages = [NEW_LABEL_1081, OLD_LAYOUT_OZON, WB_LABEL, NEW_LABEL_1080]
        self.index = build_code_index(self.pages)

    def test_by_code_single_page(self):
        page, how = resolve_page(self.pages, self.index, "46086302-0902-1", "ii50070261080")

        self.assertEqual((page, how), (3, FOUND_BY_CODE))

    def test_code_from_server_with_spaces_or_upper_case_is_normalized(self):
        page, how = resolve_page(self.pages, self.index, "46086302-0902-1", " II5007026 1081 ")

        self.assertEqual((page, how), (0, FOUND_BY_CODE))

    def test_duplicate_code_refuses_to_print(self):
        pages = [NEW_LABEL_1081, NEW_LABEL_1081]

        page, how = resolve_page(pages, build_code_index(pages), "46086302-0902-1", "ii50070261081")

        # Одним кодом нельзя определить, чья это этикетка: печатать чужую нельзя.
        self.assertEqual((page, how), (None, AMBIGUOUS_CODE))

    def test_code_absent_falls_back_to_number_for_old_layout(self):
        # Многотоварное/старое отправление: номер напечатан, разбит на две строки.
        page, how = resolve_page(self.pages, self.index, "0135822844-0538-1", "ii99999999999")

        self.assertEqual((page, how), (1, FOUND_BY_NUMBER))

    def test_wb_number_from_part_a_and_part_b(self):
        page, how = resolve_page(self.pages, self.index, "58689980160", None)

        self.assertEqual((page, how), (2, FOUND_BY_NUMBER))

    def test_no_code_and_no_number_is_not_found(self):
        self.assertEqual(resolve_page(self.pages, self.index, "11111111-1111-1", None), (None, NOT_FOUND))

    def test_code_given_but_absent_and_number_absent_is_code_not_found(self):
        page, how = resolve_page(self.pages, self.index, "11111111-1111-1", "ii12345678901")

        self.assertEqual((page, how), (None, CODE_NOT_FOUND))

    def test_partial_search_kept_from_old_logic(self):
        pages = [["Заказ 46086302-09", "02-1"]]

        self.assertEqual(find_page_by_number(pages, "46086302-0902-1"), (0, FOUND_BY_PARTIAL))


class ResolveSingleOrderPageTest(unittest.TestCase):

    def test_found_by_code(self):
        self.assertEqual(resolve_single_order_page([NEW_LABEL_1081], "46086302-0902-1", "ii50070261081"),
                         (0, FOUND_BY_CODE))

    def test_only_page_is_printed_when_nothing_matches(self):
        # PDF скачан по одному заказу — единственная страница и есть его этикетка.
        self.assertEqual(resolve_single_order_page([["новый макет без кода"]], "46086302-0902-1", None),
                         (0, FOUND_ONLY_PAGE))

    def test_several_pages_without_match_are_not_guessed(self):
        self.assertEqual(resolve_single_order_page([["a"], ["b"]], "46086302-0902-1", None), (None, NOT_FOUND))


class MessagesTest(unittest.TestCase):

    def test_messages_name_the_reason(self):
        self.assertIn("коду этикетки ii1", result_message(FOUND_BY_CODE, "Q", "ii1", 0))
        self.assertIn("стр. 3", result_message(FOUND_BY_NUMBER, "Q", None, 2))
        self.assertIn("отменена", result_message(AMBIGUOUS_CODE, "Q", "ii1", None))
        self.assertIn("Получить актуальные этикетки", result_message(CODE_NOT_FOUND, "Q", "ii1", None))
        self.assertIn("не найдена", result_message(NOT_FOUND, "Q", None, None))

    def test_normalize_code(self):
        self.assertIsNone(normalize_code(None))
        self.assertIsNone(normalize_code(""))
        self.assertIsNone(normalize_code("   "))
        self.assertEqual(normalize_code(" II 5007026\t1081 "), "ii50070261081")


@unittest.skipUnless(os.environ.get("SS_TEST_PDF"), "SS_TEST_PDF не задан — прогон на реальном PDF пропущен")
class RealPdfTest(unittest.TestCase):
    """Реальный PDF цеха: каждый код новой этикетки — ровно на одной странице и находится."""

    @classmethod
    def setUpClass(cls):
        import fitz
        doc = fitz.open(os.environ["SS_TEST_PDF"])
        cls.pages = [doc[p].get_text("text").splitlines() for p in range(len(doc))]
        cls.index = build_code_index(cls.pages)

    def test_every_code_is_on_exactly_one_page_and_resolves(self):
        self.assertTrue(self.index, "в PDF нет ни одного кода ii… — сменился макет?")
        for code, pages in self.index.items():
            self.assertEqual(len(pages), 1, f"код {code} на нескольких страницах: {pages}")
            self.assertEqual(resolve_page(self.pages, self.index, "нет-такого", code), (pages[0], FOUND_BY_CODE))

    def test_wb_page_is_still_found_by_number(self):
        wb_pages = [p for p, lines in enumerate(self.pages) if lines[:1] == ["WB"] and len(lines) >= 3]
        if not wb_pages:
            self.skipTest("в PDF нет этикеток WB")
        page = wb_pages[0]
        number = self.pages[page][1].strip() + self.pages[page][2].strip()
        found, how = resolve_page(self.pages, self.index, number, None)
        self.assertEqual(how, FOUND_BY_NUMBER)
        # Номер WB уникален: найденная страница несёт тот же номер.
        self.assertEqual(self.pages[found][1].strip() + self.pages[found][2].strip(), number)


if __name__ == "__main__":
    unittest.main()
