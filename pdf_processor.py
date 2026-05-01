"""
pdf_processor.py — PDF-манипуляции (v7.1)
==========================================
Отвечает ТОЛЬКО за работу с PDF:
  • спуск полос (booklet imposition)
  • нарезка на пачки / чанки
  • вспомогательные функции

Принцип расчёта шагов:
  Обычный документ (batch_size = страниц PDF):
    duplex → step = batch_size (чётный), chunk = chunk_size (чётный)
    simplex → step = batch_size, chunk = chunk_size

  Брошюра (batch_size = исходных страниц пользователя):
    После imposition N исходных → N/2 ландшафтных листов.
    Мы делим batch_size на 2, чтобы получить нужное кол-во
    ландшафтных листов на пачку.
    Дуплекс для брошюры всегда short (подтверждено пользователем).
    step_batch = (batch_size // 4) * 2  ← round-down-to-even of batch_size//2
    step_chunk  = (chunk_size  // 4) * 2

Верификация SNMP (prtMarkerLifeCount):
  Canon iR считает стороны (impressions), а не физические листы.
  Поэтому expected = кол-во страниц PDF-чанка (независимо от дуплекса).
  Копии уже размножены в PDF — copies передаётся, но не умножается повторно.
"""

import asyncio
import hashlib
import logging
from pathlib import Path

try:
    from pypdf import PdfReader, PdfWriter, Transformation, PageObject
    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False

log = logging.getLogger(__name__)


# ── Папка батчей (по SHA-256 контента) ───────────────────────────────────────

def get_batches_dir(pdf_path: str) -> Path:
    h = hashlib.sha256()
    try:
        with open(pdf_path, "rb") as f:
            h.update(f.read(65536))
        content_hash = h.hexdigest()[:8]
    except Exception:
        content_hash = hashlib.md5(pdf_path.encode()).hexdigest()[:8]
    stem = Path(pdf_path).stem[:20].replace(" ", "_")
    return Path(f"canon_batches_{stem}_{content_hash}")


# ══════════════════════════════════════════════════════════════════════════════
# Booklet imposition
# ══════════════════════════════════════════════════════════════════════════════

def _clone_page(page: "PageObject") -> "PageObject":
    """
    Изолированная копия страницы через PdfWriter round-trip.
    Предотвращает мутацию оригинала при transfer_rotation_to_content().
    """
    tmp = PdfWriter()
    tmp.add_page(page)
    return tmp.pages[0]


def _place_page_on_sheet(
    sheet: "PageObject",
    page: "PageObject",
    x_shift: float,
    a4_w: float,
    a4_h: float,
    safety: float,
) -> None:
    """
    Вписывает одну страницу (левую или правую) в половину ландшафтного листа.

    Использует merge_transformed_page, который создаёт Form XObject с BBox
    в локальном пространстве источника, а затем применяет матрицу.
    Клиппинг происходит в локальных координатах (до трансформации),
    поэтому контент правой страницы не отрезается.

    Клонирование страницы перед вызовом transfer_rotation_to_content()
    гарантирует, что оригинальные объекты PdfReader не мутируют.
    """
    # Клонируем, чтобы не испортить оригинал (баг #6 — мутация)
    p = _clone_page(page)
    p.transfer_rotation_to_content()

    box  = p.cropbox
    ll_x = float(box.lower_left[0])
    ll_y = float(box.lower_left[1])
    bw   = float(box.width)
    bh   = float(box.height)

    if bw == 0 or bh == 0:
        return  # пустая / невалидная страница — пропускаем

    target_w = a4_w / 2.0
    scale    = min(target_w / bw, a4_h / bh) * safety

    dx = (target_w - bw * scale) / 2.0
    dy = (a4_h    - bh * scale) / 2.0

    t = (Transformation()
         .translate(-ll_x, -ll_y)
         .scale(scale, scale)
         .translate(x_shift + dx, dy))

    # merge_transformed_page: BBox XObject = cropbox источника в локальных
    # координатах. Трансформация t применяется ПОВЕРХ → правый контент
    # не режется. (баг #1 из предыдущей версии решён именно этим методом)
    sheet.merge_transformed_page(p, t)


def _make_booklet_imposition(
    pages: list,
    binding: str = "left",
) -> "list[PageObject]":
    """
    Saddle-stitch спуск полос.
    N исходных страниц → N/2 ландшафтных листов A4.

    Порядок (binding='left', лицо/оборот для каждого листа):
      лист i:  лицо  = [n-1-2i | 2i]
               оборот = [2i+1   | n-2-2i]
    """
    A4_W   = 841.89   # A4 Landscape width  (pt)
    A4_H   = 595.28   # A4 Landscape height (pt)
    SAFETY = 0.96     # 4% margin per half

    # Добиваем до кратного 4 пустыми страницами
    remainder = len(pages) % 4
    if remainder:
        ref = pages[-1].mediabox
        w, h = float(ref.width), float(ref.height)
        for _ in range(4 - remainder):
            pages.append(PageObject.create_blank_page(width=w, height=h))

    n      = len(pages)
    result = []

    for i in range(n // 4):
        f_l, f_r = n - 1 - 2*i,   2*i
        b_l, b_r = 2*i + 1,        n - 2 - 2*i

        if binding == "right":
            f_l, f_r = f_r, f_l
            b_l, b_r = b_r, b_l

        for left_idx, right_idx in [(f_l, f_r), (b_l, b_r)]:
            sheet = PageObject.create_blank_page(width=A4_W, height=A4_H)
            _place_page_on_sheet(sheet, pages[left_idx],  0.0,      A4_W, A4_H, SAFETY)
            _place_page_on_sheet(sheet, pages[right_idx], A4_W/2.0, A4_W, A4_H, SAFETY)

            # Финальная фиксация рамок результирующего листа
            sheet.mediabox.lower_left  = (0, 0)
            sheet.mediabox.upper_right = (A4_W, A4_H)
            sheet.cropbox = sheet.mediabox
            result.append(sheet)

    return result


# ══════════════════════════════════════════════════════════════════════════════
# Расчёт шагов
# ══════════════════════════════════════════════════════════════════════════════

def _calc_steps(
    batch_size: int,
    chunk_size: int,
    duplex: str,
    is_booklet: bool,
) -> tuple[int, int]:
    """
    Возвращает (step_batch, step_chunk) — шаги по массиву готовых к печати страниц.

    В случае брошюры массив pages уже содержит скомпонованные А4 страницы.
    Для идеальной нарезки без остатков и сбоев принтера округляем значения 
    до ближайшего кратного 8 (например: batch 60 -> 64, chunk 30 -> 32).
    """
    if is_booklet:
        step_b = max(8, int(round(batch_size / 8)) * 8)
        step_c = max(8, int(round(chunk_size / 8)) * 8)
    else:
        if duplex != "none":
            step_b = max(2, int(round(batch_size / 2)) * 2)
            step_c = max(2, int(round(chunk_size / 2)) * 2)
        else:
            step_b = max(1, batch_size)
            step_c = max(1, chunk_size)

    return step_b, step_c

# ══════════════════════════════════════════════════════════════════════════════
# Нарезка (синхронная)
# ══════════════════════════════════════════════════════════════════════════════

def _split_pdf_sync(
    pdf_path:     str,
    batch_size:   int,
    chunk_size:   int,
    duplex:       str,
    copies:       int,
    batches_dir:  Path,
    booklet_mode: str = "none",
) -> "list[list[Path]]":
    if not PYPDF_AVAILABLE:
        raise ImportError("pip install pypdf")

    batches_dir.mkdir(parents=True, exist_ok=True)
    reader = PdfReader(pdf_path)
    is_booklet = booklet_mode in ("left", "right")
    pages: list = list(reader.pages)

    if is_booklet:
        log.info(f"Спуск полос для брошюры ({booklet_mode})...")
        pages = _make_booklet_imposition(pages, binding=booklet_mode)
        duplex = "short"

    total_per_copy = len(pages)
    step_b, step_c = _calc_steps(batch_size, chunk_size, duplex, is_booklet)

    result: list[list[Path]] = []
    current_batch = []
    current_pages = 0
    global_batch_num = 1

    # Динамическая нарезка: заполняем пачки ровно до лимита step_b
    for copy_idx in range(1, copies + 1):
        c_start = 0
        while c_start < total_per_copy:
            # Сколько страниц можем добавить в текущую пачку до лимита?
            space_in_batch = step_b - current_pages
            
            # Если пачка почему-то заполнилась, закрываем её
            if space_in_batch <= 0:
                result.append(current_batch)
                current_batch = []
                current_pages = 0
                global_batch_num += 1
                space_in_batch = step_b

            # Высчитываем размер следующего чанка: 
            # Не больше лимита чанка (32), не больше остатка в пачке (space_in_batch), не больше остатка в копии
            chunk_pages = min(step_c, space_in_batch, total_per_copy - c_start)
            c_end = c_start + chunk_pages

            chunk_path = (
                batches_dir 
                / f"cp{copy_idx:02d}_p{c_start+1:04d}-{c_end:04d}.pdf"
            )

            if not chunk_path.exists():
                writer = PdfWriter()
                for idx in range(c_start, c_end):
                    writer.add_page(pages[idx])

                # DUPLEX GUARD: Защита от печати на обороте стыка копий
                if duplex != "none" and len(writer.pages) % 2 != 0:
                    ref = pages[0].mediabox
                    writer.add_blank_page(width=float(ref.width), height=float(ref.height))

                with open(chunk_path, "wb") as f:
                    writer.write(f)

            actual_pages = len(PdfReader(str(chunk_path)).pages)
            current_batch.append(chunk_path)
            current_pages += actual_pages

            log.info(
                f"  Копия {copy_idx}/{copies} | Чанк {c_start+1}–{c_end} "
                f"({actual_pages} стр.) -> В пачке {global_batch_num}: {current_pages}/{step_b}"
            )

            c_start = c_end

            # Если пачка достигла лимита (например, 64) — закрываем её
            if current_pages >= step_b:
                result.append(current_batch)
                current_batch = []
                current_pages = 0
                global_batch_num += 1

    # Если остался хвост (самая последняя пачка сессии)
    if current_batch:
        result.append(current_batch)

    log.info(f"Итого сформировано пачек охлаждения: {len(result)}")
    return result

# ── Async обёртка ─────────────────────────────────────────────────────────────

async def split_pdf_into_batches(
    pdf_path:     str,
    batch_size:   int,
    chunk_size:   int,
    duplex:       str,
    copies:       int,
    batches_dir:  Path,
    booklet_mode: str = "none",
) -> "list[list[Path]]":
    """Async обёртка: нарезка в thread-pool, не блокирует event loop."""
    return await asyncio.to_thread(
        _split_pdf_sync,
        pdf_path, batch_size, chunk_size, duplex,
        copies, batches_dir, booklet_mode,
    )


# ── Вспомогательные функции ───────────────────────────────────────────────────

def get_total_pages(pdf_path: str) -> int:
    """Быстро возвращает число страниц PDF."""
    if not PYPDF_AVAILABLE:
        return 0
    try:
        return len(PdfReader(pdf_path).pages)
    except Exception:
        return 0


def expected_pages_for_batch(batch_path: Path) -> int:
    """
    Ожидаемый прирост SNMP-счётчика Canon за этот чанк.

    Canon iR считает impressions (стороны):
      • обычный дуплекс 60 стр. PDF → 60 impressions ✓
      • брошюра 30 ландшафтных стр. duplex short → 30 impressions ✓

    Копии уже размножены в PDF на этапе нарезки, поэтому
    copies здесь не передаётся и не умножается.
    """
    if not PYPDF_AVAILABLE:
        return 0
    try:
        return len(PdfReader(str(batch_path)).pages)
    except Exception:
        return 0


def cleanup_batches(batches: "list[list[Path]]") -> None:
    ok = 0
    for batch in batches:
        for b in batch:
            try:
                b.unlink(missing_ok=True)
                ok += 1
            except Exception:
                pass
    log.info(f"Временные файлы удалены: {ok}")
