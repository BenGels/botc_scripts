"""Generate a themed emblem logo for a script via a pluggable image-generation provider.

Two providers are supported:
- "pollinations" (default): Pollinations.ai's free, keyless image API. No account or API key
  needed - great default so the script works out of the box.
- "openai": OpenAI's Images API (paid, needs OPENAI_API_KEY / billing credits). Supports
  (only this provider) a local reference image.

Regardless of provider, the result is post-processed to guarantee a transparent background: if
the image doesn't already have meaningful transparency (neither gpt-image-2 nor Pollinations/Flux
can produce one), we run it through ``rembg`` to strip the background locally - no extra API calls
or cost.
"""
# Standard Library
import base64
import io
import os
import sys
import time
import urllib.parse
from pathlib import Path

# Third Party
import requests
from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image
from rich import print

# Automatically pick up an OPENAI_API_KEY (or anything else) from a .env file, searching the
# current directory and its parents. Does nothing (silently) if no .env file is found, and never
# overrides a variable that's already set in the real environment. Only relevant when using the
# "openai" provider.
load_dotenv()

# The scene is the star of the show (a literal, narrative illustration - not an abstract symbol),
# rendered in a consistent vintage-emblem frame. {extra_clause} is the user's extra description
# (the concrete scene to depict); {name} is the script's title.
BASE_PROMPT = (
    "Create a highly detailed vintage illustrated emblem in the style of a 19th-century Victorian engraving, antique etching, and old nautical/apothecary woodcut. The artwork should feel like an illustration from a mysterious, slightly macabre tabletop game publication: elegant, theatrical, sinister, whimsical, and humorous rather than genuinely horrific. Use a limited dark navy / blue-black ink palette on warm aged parchment, with extremely fine engraving linework, dense cross-hatching, stippling, engraved shadows, scratchy ink textures, and intricate woodcut-like detailing. Avoid modern digital painting aesthetics, gradients, photorealism, glossy surfaces, or clean vector graphics.The composition should be designed as a self-contained vintage emblem or logo, usually contained within an ornate circular or oval frame. Surround the artwork with elaborate Victorian ornamental flourishes, curling filigree, decorative compass points, gothic motifs, distressed borders, and small symmetrical embellishments. The frame should feel slightly worn and imperfect, as if printed on an old game box or illustrated pamphlet. Characters and objects should have strong silhouettes and exaggerated expressions, with a charming grotesque quality. Creatures may be mischievous, suspicious, absurd, theatrical, or cartoonishly sinister. Give them expressive faces, exaggerated anatomy, dramatic poses, and richly detailed costumes while retaining the engraved illustration aesthetic. Use dramatic central composition, layered foreground and background elements, carefully balanced symmetry, and strong visual hierarchy. Important objects should remain immediately recognizable even at small logo size. Typography, when requested, should use ornate Victorian blackletter / gothic display lettering, integrated naturally into the illustration rather than looking like modern graphic-design text. Decorative swashes and small stars may surround the lettering. The final result should resemble a beautifully printed antique occult board-game emblem: sophisticated, detailed, mysterious, darkly humorous, slightly nautical, and deliberately old-fashioned. "
    "Create an emblem titled '{name}'.The central scene depicts {extra_clause}."
    "The image shall hold no text or letters. The illustration must be within a round frame and the background around this frame must be transparent"
    "Create the artwork as an isolated foreground emblem with a fully transparent background. Preserve every foreground subject, decorative element, text element, and border completely intact. Use clean, smooth cutout edges around the entire illustration. Do not add parchment, paper texture, scenery, shadows, or any colored background outside the artwork. The negative space between individual foreground elements should also remain transparent where appropriate. Retain the antique navy engraved ink appearance and distressed printed texture inside the artwork itself."
    "Keep the entire illustration visually consistent with the Master Style: intricate engraving linework, dense cross-hatching, stippling, dark navy ink, aged-paper aesthetic, ornate gothic border, distressed antique printing texture, and whimsical grotesque humor. The image should work as a recognizable standalone logo/emblem, with a strong silhouette and readable composition even when reduced in size."
    "KEYWORDS: Victorian engraving, antique etching, gothic emblem, nautical engraving, 19th-century woodcut, cross-hatching, stippling, engraved illustration, dark navy ink, aged parchment, distressed print, ornate filigree, gothic blackletter, grotesque whimsical characters, occult Victorian aesthetic, antique tabletop game illustration, highly detailed line art."
)

DEFAULT_PROVIDER = "pollinations"
DEFAULT_OPENAI_MODEL = "gpt-image-2"


def build_prompt(name, extra_description=""):
    """Build the final image prompt from the script name and the (recommended) scene description."""
    return BASE_PROMPT.format(name=name, extra_clause=extra_description)


def ask_extra_description():
    """Interactively ask the user for an optional extra description for the logo.

    Returns "" (and prints a note) if input isn't available, e.g. when run non-interactively, or
    if the terminal sends text in an encoding stdin can't decode.
    """
    # Some terminals/locales leave stdin on a non-UTF-8 codec, which makes input() raise
    # UnicodeDecodeError on non-ASCII text (umlauts, "smart quotes" from copy-paste, etc.).
    # Force UTF-8 with a safe fallback so a decoding hiccup never crashes the whole run.
    if hasattr(sys.stdin, "reconfigure"):
        try:
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    try:
        extra = input(
            "Zusaetzliche Beschreibung fuer das Logo (empfohlen - z.B. eine konkrete Szene; "
            "Enter zum Ueberspringen): "
        ).strip()
    except EOFError:
        print("[yellow]Hinweis:[/] Kein interaktives Terminal erkannt, ueberspringe Logo-Zusatzbeschreibung.")
        extra = ""
    except UnicodeDecodeError:
        print("[yellow]Warnung:[/] Eingabe konnte nicht sauber als UTF-8 gelesen werden "
              "(Terminal-Kodierung?). Ueberspringe Logo-Zusatzbeschreibung.")
        extra = ""
    return extra


def _generate_pollinations(prompt, size="1024x1024", retries=2, timeout=120):
    """Generate an image via Pollinations.ai's free, keyless image API. Returns raw image bytes."""
    width, height = size.split("x")
    encoded_prompt = urllib.parse.quote(prompt)
    url = (
        f"https://image.pollinations.ai/prompt/{encoded_prompt}"
        f"?width={width}&height={height}&model=flux&nologo=true"
    )

    last_error = None
    for attempt in range(1, retries + 2):
        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            if not response.content:
                raise RuntimeError("Pollinations.ai returned an empty response.")
            return response.content
        except (requests.RequestException, RuntimeError) as e:
            last_error = e
            if attempt <= retries:
                print(f"[yellow]Logo-Generierung (Pollinations.ai) fehlgeschlagen, versuche erneut "
                      f"({attempt}/{retries})...[/]")
                time.sleep(2)

    raise RuntimeError(f"Pollinations.ai Logo-Generierung fehlgeschlagen: {last_error}")


def _generate_openai(prompt, size="1024x1024", model=DEFAULT_OPENAI_MODEL, reference_image=None):
    """Generate an image via the (paid) OpenAI Images API. Returns raw image bytes.

    If reference_image is given, uses the images.edit endpoint so the reference actually guides
    the result (style/composition/subject), instead of images.generate which ignores it.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY ist nicht gesetzt. Bitte als Umgebungsvariable (oder in einer .env-Datei) "
            "setzen, bevor das Skript ausgefuehrt wird, z.B.: export OPENAI_API_KEY=sk-..."
        )

    client = OpenAI(api_key=api_key)

    if reference_image:
        reference_image = Path(reference_image)
        if not reference_image.is_file():
            raise RuntimeError(f"Referenzbild nicht gefunden: {reference_image}")
        print(f"[green]Nutze Referenzbild:[/] {reference_image}")
        with open(reference_image, "rb") as ref_file:
            response = client.images.edit(
                model=model, image=ref_file, prompt=prompt, size=size, n=1,
            )
    else:
        response = client.images.generate(
            model=model, prompt=prompt, size=size, n=1,
        )
    return base64.b64decode(response.data[0].b64_json)


def _has_real_transparency(image_bytes, sample_threshold=0.02):
    """Check whether an image already has a meaningfully transparent background."""
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    except Exception:
        return False
    alpha = img.getchannel("A")
    histogram = alpha.histogram()
    fully_transparent_pixels = histogram[0]
    total_pixels = img.width * img.height
    return (fully_transparent_pixels / total_pixels) > sample_threshold


def _strip_background(image_bytes):
    """Remove the background locally (free, no API) using rembg. Returns PNG bytes with alpha."""
    from rembg import remove  # Imported lazily - loads/verifies the ML model on first use.
    return remove(image_bytes)


def _ensure_transparent(image_bytes):
    """Make sure the final logo has a transparent background, cutting it out locally if needed."""
    if _has_real_transparency(image_bytes):
        return image_bytes
    print("[green]Entferne Logo-Hintergrund lokal (rembg)...[/]")
    try:
        return _strip_background(image_bytes)
    except Exception as e:
        print(f"[yellow]Warnung:[/] Hintergrund-Entfernung fehlgeschlagen ({e}); "
              "verwende das Logo mit undurchsichtigem Hintergrund.")
        return image_bytes


def generate_logo(name, output_path, extra_description="", size="1024x1024",
                  provider=DEFAULT_PROVIDER, model=DEFAULT_OPENAI_MODEL, reference_image=None):
    """Generate a logo image for the given script name and save it as a transparent PNG.

    Args:
        name (str): The script name.
        output_path (str|Path): Where to save the generated PNG.
        extra_description (str): A concrete scene description - strongly recommended, this is
            what actually drives a specific, illustrated result rather than a generic emblem.
        size (str): Image size, e.g. "1024x1024".
        provider (str): "pollinations" (free, default) or "openai" (paid).
        model (str): The OpenAI image model to use (only relevant for provider="openai").
        reference_image (str|Path|None): Path to a local image to use as a style/composition
            reference. Only honored for provider="openai" (uses the images.edit endpoint); if set
            with provider="pollinations", it's ignored with a warning, since Pollinations' image
            API only accepts reference images by public URL, not local files.

    Returns:
        Path: The path the logo was saved to.

    Raises:
        RuntimeError: If generation fails, or (for "openai") if OPENAI_API_KEY isn't set.
        ValueError: If an unknown provider is given.
    """
    output_path = Path(output_path)
    prompt = build_prompt(name, extra_description)
    print(f"[green]Generiere Logo ueber {provider}...[/] (prompt: {prompt[:100]}...)")

    if provider == "pollinations":
        if reference_image:
            print("[yellow]Warnung:[/] Referenzbilder werden vom kostenlosen Pollinations-Provider "
                  "nicht unterstuetzt (nur ueber --logo-provider openai). Wird ignoriert.")
        image_bytes = _generate_pollinations(prompt, size=size)
    elif provider == "openai":
        image_bytes = _generate_openai(prompt, size=size, model=model, reference_image=reference_image)
    else:
        raise ValueError(f"Unbekannter Logo-Provider: '{provider}'. Erlaubt: 'pollinations', 'openai'.")

    image_bytes = _ensure_transparent(image_bytes)
    output_path.write_bytes(image_bytes)
    return output_path