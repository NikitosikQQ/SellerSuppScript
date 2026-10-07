"""Наклейка пилы: QR заказа (с номером текстом под ним) и склейка нескольких наклеек в один PDF.

Наклейка рисуется сразу в размер этикетки 58×40 мм (как LABEL_WIDTH_MM/LABEL_HEIGHT_MM в
seller_supp_api): print_pdf подгоняет страницу под этикетку, и для страницы ровно этого размера
масштаб равен 1. Номер под QR нужен, чтобы наклейку, вышедшую не на ту деталь, было видно сразу:
в очереди печати наклейки выходят пачками, а QR глазами не прочитать.
"""
import io

import fitz
import qrcode

MM = 72.0 / 25.4
LABEL_W_MM, LABEL_H_MM = 58, 40
_FONT = "hebo"  # Helvetica Bold из встроенных шрифтов PDF: цифры и дефис, кириллица не нужна


def _qr_png(text, border=4):
    qr = qrcode.QRCode(border=border)
    qr.add_data(text)
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image().convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def build_label_pdf(text, path, with_text=True):
    """Одностраничный PDF наклейки. with_text=False — прежняя наклейка 26.10.5: только QR на
    странице 38×38 мм (под 58×40 её подгоняет print_pdf)."""
    doc = fitz.open()
    try:
        if not with_text:
            size = 38 * MM
            page = doc.new_page(width=size, height=size)
            margin = 5
            side = size - 2 * margin
            page.insert_image(fitz.Rect(margin, margin, margin + side, margin + side), stream=_qr_png(text))
        else:
            w, h = LABEL_W_MM * MM, LABEL_H_MM * MM
            page = doc.new_page(width=w, height=h)
            # QR 31 мм сверху по центру (рамка тишины 2 модуля — по бокам и так 13 мм белого),
            # под ним полоса 8 мм под номер.
            side = 31 * MM
            x0, y0 = (w - side) / 2, 0.5 * MM
            page.insert_image(fitz.Rect(x0, y0, x0 + side, y0 + side), stream=_qr_png(text, border=2))
            max_width = w - 4 * MM
            fontsize = 11.0
            while fontsize > 6 and fitz.get_text_length(text, fontname=_FONT, fontsize=fontsize) > max_width:
                fontsize -= 0.5
            text_w = fitz.get_text_length(text, fontname=_FONT, fontsize=fontsize)
            band_top, band_h = y0 + side, 8 * MM
            baseline = band_top + (band_h + fontsize * 0.72) / 2  # 0.72 — высота прописных Helvetica
            page.insert_text(((w - text_w) / 2, baseline), text, fontname=_FONT, fontsize=fontsize)
        doc.save(path)
    finally:
        doc.close()


def merge_pdfs(paths, out_path):
    """Склеивает PDF в один в заданном порядке (порядок страниц = порядок сканов)."""
    out = fitz.open()
    try:
        for p in paths:
            src = fitz.open(p)
            try:
                out.insert_pdf(src)
            finally:
                src.close()
        out.save(out_path)
    finally:
        out.close()
