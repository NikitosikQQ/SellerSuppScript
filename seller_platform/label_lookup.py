"""Поиск страницы этикетки в PDF маркетплейса — без Qt, чтобы тестироваться отдельно.

Копия корневого label_lookup.py цехового скрипта seller-support (25c8bfc): менять синхронно.

С 05.10.2026 на новой этикетке OZON нет номера отправления — только номер экземпляра
товара «ii…» (поле scanit, в тексте PDF: «ii5007026 1081»). Сервер отдаёт этот код в
ответе валидации (labelCode), и страница ищется по нему. Если кода нет или в PDF его нет —
прежний поиск по номеру заказа: старые макеты OZON, WB, ЯМ.
"""
import re

# Строка с кодом новой этикетки: «ii5007026 1081» — последние 4 цифры отделены плашкой.
_CODE_LINE = re.compile(r"ii[0-9a-z]+(?:\s+[0-9a-z]+)*", re.IGNORECASE)
# Строка, похожая на номер заказа: цифры, дефисы, пробелы (как в прежнем поиске).
_NUMBER_LINE = re.compile(r"[0-9\- ]+")

FOUND_BY_CODE = "code"
FOUND_BY_NUMBER = "number"
FOUND_BY_PARTIAL = "partial"
FOUND_ONLY_PAGE = "only_page"
AMBIGUOUS_CODE = "ambiguous_code"
CODE_NOT_FOUND = "code_not_found"
NOT_FOUND = "not_found"


def normalize_code(code):
    """Код без пробелов в нижнем регистре; None, если пусто."""
    if not code:
        return None
    normalized = re.sub(r"\s+", "", code).lower()
    return normalized or None


def build_code_index(pages_text):
    """Код этикетки → номера страниц (с нуля) по строкам «ii…» текстового слоя PDF."""
    index = {}
    for page_num, lines in enumerate(pages_text):
        page_codes = set()
        for line in lines:
            stripped = line.strip()
            if _CODE_LINE.fullmatch(stripped):
                page_codes.add(normalize_code(stripped))
        for code in page_codes:
            index.setdefault(code, []).append(page_num)
    return index


def find_page_by_number(pages_text, query):
    """Прежний поиск по номеру заказа — без изменений, вынесен из виджетов.

    Сначала строки из цифр/дефисов и склейки соседних (OZON разбивал номер на две строки,
    WB печатает partA и partB отдельно), сравнение и без пробелов. Затем частичный поиск:
    начало номера в строке и последние 4 символа отдельным фрагментом на той же странице.
    Возвращает (страница, способ) или (None, None).
    """
    query_lower = query.lower()
    query_nospace = query.replace(" ", "").lower()
    for page_num, text in enumerate(pages_text):
        filtered = [line.strip() for line in text if _NUMBER_LINE.fullmatch(line.strip())]
        joined_pairs = [filtered[i] + filtered[i + 1] for i in range(len(filtered) - 1)]
        for line in filtered + joined_pairs:
            clean_line = line.split(" ", 1)[0]
            if clean_line.lower() == query_lower or line.replace(" ", "").lower() == query_nospace:
                return page_num, FOUND_BY_NUMBER

    if len(query) > 4:
        saved_suffix = query[-4:].lower()
        short_query = query[:-4].lower()
        pages = sorted({page_num for page_num, text in enumerate(pages_text)
                        if any(short_query in line.lower() for line in text)})
        for page_num in pages:
            for fragment in " ".join(pages_text[page_num]).split():
                frag = fragment.strip()
                if len(frag) == 4 and frag.lower() == saved_suffix:
                    return page_num, FOUND_BY_PARTIAL
    return None, None


def resolve_page(pages_text, code_index, query, label_code):
    """Страница этикетки для скана: (страница | None, способ или причина отказа).

    Код этикетки, если он есть, решает однозначно: ровно одна страница — она; несколько —
    отказ (одним кодом нельзя определить, чья этикетка, а печатать чужую нельзя). Кода нет
    или он не найден — прежний поиск по номеру.
    """
    code = normalize_code(label_code)
    if code:
        pages = code_index.get(code, [])
        if len(pages) == 1:
            return pages[0], FOUND_BY_CODE
        if len(pages) > 1:
            return None, AMBIGUOUS_CODE
    page, how = find_page_by_number(pages_text, query)
    if page is not None:
        return page, how
    return None, (CODE_NOT_FOUND if code else NOT_FOUND)


def resolve_single_order_page(pages_text, query, label_code):
    """То же для PDF, скачанного по одному заказу («Скачать и распечатать»).

    Сервер отдаёт в нём этикетку только этого заказа, поэтому единственная страница — она,
    даже если ни код, ни номер в тексте не нашлись (на случай очередной смены макета).
    """
    page, how = resolve_page(pages_text, build_code_index(pages_text), query, label_code)
    if page is None and how != AMBIGUOUS_CODE and len(pages_text) == 1:
        return 0, FOUND_ONLY_PAGE
    return page, how


def result_message(how, query, label_code, page):
    """Сообщение в консоль виджета по итогу поиска."""
    if how == FOUND_BY_CODE:
        return f"✅ Найдено по коду этикетки {label_code}: {query} на стр. {page + 1}"
    if how == FOUND_BY_NUMBER:
        return f"✅ Найдено: {query} на стр. {page + 1}"
    if how == FOUND_BY_PARTIAL:
        return f"✅ Найдено (частичный поиск): {query} на стр. {page + 1}"
    if how == FOUND_ONLY_PAGE:
        return f"✅ Этикетка заказа {query} (единственная страница)"
    if how == AMBIGUOUS_CODE:
        return (f"⛔ Код этикетки {label_code} есть на нескольких страницах PDF — "
                f"печать отменена, сообщите менеджеру")
    if how == CODE_NOT_FOUND:
        return (f"⚠️ Этикетка {query} (код {label_code}) не найдена в загруженном PDF — "
                f"нажмите «Получить актуальные этикетки»")
    return f"⚠️ Строка {query} не найдена."
