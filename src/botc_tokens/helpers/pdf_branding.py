"""Overlay a name and logo onto the generated PDFs, then merge, flatten and optimize them.

All pixel coordinates the user supplies are given in the "paper" pixel space that ``Printable``
uses (e.g. 2550x3300px for a default US Letter page at 300dpi). To convert those to PDF points we
measure the *actual* rendered page size (``page.rect``) and scale accordingly, rather than
assuming a fixed DPI - this way it doesn't matter exactly how wand/ImageMagick chose to write the
page size, the overlay always lines up with the coordinates the user specified.
"""
# Standard Library
from pathlib import Path
import shutil
import subprocess

# Third Party
import fitz  # PyMuPDF
import pikepdf
from rich import print


def _scale_factors(page, paper_width_px, paper_height_px):
    """Return (x_scale, y_scale) to convert paper-pixel coordinates to this page's PDF points."""
    return page.rect.width / paper_width_px, page.rect.height / paper_height_px


def _wrap_lines(font, text, font_size, max_width):
    """Greedily wrap text into lines that each fit within max_width at the given font size."""
    words = text.split()
    if not words:
        return [""]
    lines = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if font.text_length(candidate, fontsize=font_size) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _fit_and_wrap(font, text, max_width, max_height, max_font_size, min_size=6, line_spacing=1.2):
    """Find the largest font size (<= max_font_size) whose wrapped lines fit within
    max_width x max_height. Returns (font_size, lines, line_height)."""
    size = max_font_size
    while size > min_size:
        lines = _wrap_lines(font, text, size, max_width)
        line_height = size * line_spacing
        if line_height * len(lines) <= max_height:
            return size, lines, line_height
        size -= 1
    lines = _wrap_lines(font, text, min_size, max_width)
    return min_size, lines, min_size * line_spacing


def _draw_name_wrapped(page, text, fontfile, x, y, width, height, sx, sy, color=(0, 0, 0)):
    """Draw text inside a fixed box, wrapping lines, auto-shrinking to fit, and centered both
    horizontally and vertically within the box.

    Lines are laid out and drawn manually (rather than via insert_textbox's built-in box
    layout) so the vertical centering math is exact and doesn't depend on insert_textbox's
    internal "unused space" accounting.
    """
    left, top = x * sx, y * sy
    box_width, box_height = width * sx, height * sy
    font = fitz.Font(fontfile=str(fontfile))

    size, lines, line_height = _fit_and_wrap(font, text, box_width, box_height, max_font_size=box_height)

    text_block_height = line_height * len(lines)
    # insert_text() positions by baseline, not top-left, so offset the first line's baseline
    # down by the font's ascent (approximated as 0.8 * size, true for the vast majority of
    # fonts) from the top of the centered block.
    block_top = top + (box_height - text_block_height) / 2
    baseline = block_top + size * 0.8

    for line in lines:
        line_width = font.text_length(line, fontsize=size)
        line_left = left + (box_width - line_width) / 2
        page.insert_text((line_left, baseline), line, fontsize=size, fontfile=str(fontfile),
                        fontname="namefont", color=color)
        baseline += line_height


def _draw_name_single_line(page, text, fontfile, x, y, font_height, sx, sy, color=(0, 0, 0)):
    """Draw a single line of text at a fixed font size, anchored at the top-left corner."""
    left, top = x * sx, y * sy
    size = font_height * sy
    rect = fitz.Rect(left, top, page.rect.width - 10, top + size * 1.4)
    page.insert_textbox(
        rect, text, fontsize=size, fontfile=str(fontfile), fontname="namefont",
        align=fitz.TEXT_ALIGN_LEFT, color=color,
    )


def _draw_logo(page, logo_path, x, y, width, height, sx, sy):
    rect = fitz.Rect(x * sx, y * sy, (x + width) * sx, (y + height) * sy)
    page.insert_image(rect, filename=str(logo_path))


def brand_pdf(pdf_path, name, fontfile, logo_path, name_box, logo_box,
              paper_width_px=2550, paper_height_px=3300, name_color=(0, 0, 0)):
    """Overlay the name text and logo onto every page of a generated PDF, in place.

    Args:
        pdf_path (str|Path): PDF to modify.
        name (str): The text to draw (the script name).
        fontfile (str|Path): Path to the TTF font to use for the name.
        logo_path (str|Path): Path to the generated logo PNG.
        name_box (dict): ``{"xy": (x, y), "height": h}`` for a single fixed-size line, or
            ``{"xy": (x, y), "width": w, "height": h}`` for a wrapped, auto-shrunk block (presence
            of "width" is what selects box mode - no separate "wrap" flag needed).
            All values are in the same paper-pixel space as ``paper_width_px``/``paper_height_px``.
        logo_box (dict): ``{"xy": (x, y), "width": w, "height": h}``, same pixel space.
        paper_width_px (int): The pixel width the coordinates above are relative to (i.e. the
            ``--paper-width`` the sheets were generated with).
        paper_height_px (int): Same, for height (``--paper-height``).
        name_color (tuple): (r, g, b) floats in [0, 1] for the name text color. Defaults to black.
            Only affects this overlaid name text - never the role/ability text inside scriptblocks,
            which is rendered through a completely separate path (wand/text_tools.py).
    """
    pdf_path = Path(pdf_path)
    doc = fitz.open(pdf_path)
    for page in doc:
        sx, sy = _scale_factors(page, paper_width_px, paper_height_px)
        _draw_logo(page, logo_path, *logo_box["xy"], logo_box["width"], logo_box["height"], sx, sy)
        # A box with a "width" is treated as a bounded area (wrap lines + auto-shrink to fit);
        # one with only a "height" is a single fixed-size line anchored at its top-left corner.
        if "width" in name_box:
            _draw_name_wrapped(page, name, fontfile, *name_box["xy"],
                              name_box["width"], name_box["height"], sx, sy, color=name_color)
        else:
            _draw_name_single_line(page, name, fontfile, *name_box["xy"], name_box["height"], sx, sy,
                                  color=name_color)

    # Use a full (non-incremental) save to a temp file, then swap it in. Incremental saves
    # (saveIncr) can silently no-op on PDFs with certain xref structures (e.g. some
    # ImageMagick/wand output) - a full rewrite is slower but always reliably persists changes.
    tmp_path = pdf_path.with_suffix(".branded.tmp.pdf")
    doc.save(tmp_path)
    doc.close()
    tmp_path.replace(pdf_path)


def _ghostscript_recompress(pdf_path):
    """Re-run a PDF through Ghostscript's pdfwrite device, in place.

    pikepdf's ``compress_streams`` only Flate-compresses the raw pixel data of the rasterized
    pages, which is lossless and huge for photographic content (tens of MB). Ghostscript's
    pdfwrite device re-encodes those same images as JPEG at its default quality instead, which
    is dramatically smaller (16MB -> ~1MB in testing) at the same pixel resolution, with no
    visible quality loss for print. Requires the `gs` binary; skipped with a warning if missing.
    """
    gs = shutil.which("gs")
    if gs is None:
        print("[yellow]Warning:[/] Ghostscript ('gs') not found on PATH - skipping the extra "
              "compression pass. Install Ghostscript for a much smaller final PDF.")
        return

    pdf_path = Path(pdf_path)
    tmp_path = pdf_path.with_suffix(".gs.tmp.pdf")
    result = subprocess.run(
        [gs, "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
         "-dPreserveAnnots=false", f"-sOutputFile={tmp_path}", str(pdf_path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"[yellow]Warning:[/] Ghostscript compression failed, keeping the larger PDF: "
              f"{result.stderr.strip()}")
        tmp_path.unlink(missing_ok=True)
        return

    tmp_path.replace(pdf_path)


def merge_flatten_and_optimize(pdf_paths_in_order, output_path, dpi=300):
    """Merge PDFs in the given order, flatten every page to a single raster layer, and compress.

    Flattening (re-rendering each page to an image and rebuilding the PDF from that) guarantees a
    single, print-safe content layer per page - no risk of font-embedding or overlapping-layer
    issues from the text/logo overlay step.

    Args:
        pdf_paths_in_order (list[str|Path]): PDFs to merge, in the desired final order.
        output_path (str|Path): Where to write the final PDF.
        dpi (int): Resolution to rasterize pages at before rebuilding the PDF.
    """

    flattened = fitz.open()
    for path in pdf_paths_in_order:
        with fitz.open(path) as src:
            for page in src:
                rect = page.rect
                pix = page.get_pixmap(dpi=dpi)
                new_page = flattened.new_page(width=rect.width, height=rect.height)
                new_page.insert_image(rect, pixmap=pix)

    tmp_path = Path(output_path).with_suffix(".tmp.pdf")
    flattened.save(tmp_path, garbage=4, deflate=True, clean=True)
    flattened.close()

    with pikepdf.open(tmp_path) as pdf:
        pdf.remove_unreferenced_resources()
        pdf.save(
            output_path,
            compress_streams=True,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
            linearize=True,
        )
    tmp_path.unlink(missing_ok=True)

    _ghostscript_recompress(output_path)
    print(f"[green]Finales PDF gespeichert:[/] {output_path}")