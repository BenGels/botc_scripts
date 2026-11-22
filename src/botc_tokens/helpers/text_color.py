"""Parse and interactively ask for a hex color code used for the script-name text overlay.

This only ever affects the name text drawn onto script.pdf/backside.pdf by pdf_branding - it has
no effect on the role/ability text rendered inside the scriptblocks (that's a fully separate path
through wand/text_tools.py).
"""
# Standard Library
import re
import sys

# Third Party
from rich import print

BLACK = (0, 0, 0)

_HEX_COLOR_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def parse_hex_color(value):
    """Parse a hex color string ("#RRGGBB" or "RRGGBB") into a fitz-style (r, g, b) float tuple.

    Returns None if value is empty/None or isn't a valid 6-digit hex color - callers should treat
    None as "fall back to black".
    """
    if not value:
        return None
    match = _HEX_COLOR_RE.match(value.strip())
    if not match:
        return None
    hex_str = match.group(1)
    r = int(hex_str[0:2], 16) / 255
    g = int(hex_str[2:4], 16) / 255
    b = int(hex_str[4:6], 16) / 255
    return (r, g, b)


def ask_name_color():
    """Interactively ask for an optional hex color code for the script-name text.

    Returns the raw string entered (possibly invalid/empty - validate separately with
    parse_hex_color), or "" if input isn't available.
    """
    # Same UTF-8 safety net as the other interactive prompts: some terminals/locales leave stdin
    # on a non-UTF-8 codec, which would otherwise raise UnicodeDecodeError on unexpected input.
    if hasattr(sys.stdin, "reconfigure"):
        try:
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    try:
        value = input(
            "Schriftfarbe fuer den Scriptnamen als Hex-Code (z.B. #FFFFFF, optional, "
            "Enter fuer Schwarz): "
        ).strip()
    except EOFError:
        print("[yellow]Hinweis:[/] Kein interaktives Terminal erkannt, verwende Schwarz fuer den Scriptnamen.")
        value = ""
    except UnicodeDecodeError:
        print("[yellow]Warnung:[/] Eingabe konnte nicht sauber als UTF-8 gelesen werden, "
              "verwende Schwarz fuer den Scriptnamen.")
        value = ""
    return value


def resolve_name_color(raw_value):
    """Validate a raw hex color string, warning and falling back to black if it's invalid.

    Empty/None input falls back to black silently (that's just "no color specified").
    """
    if not raw_value:
        return BLACK
    color = parse_hex_color(raw_value)
    if color is None:
        print(f"[yellow]Warnung:[/] '{raw_value}' ist kein gueltiger Hex-Farbcode (erwartet z.B. '#FFFFFF'), "
              "verwende Schwarz fuer den Scriptnamen.")
        return BLACK
    return color
