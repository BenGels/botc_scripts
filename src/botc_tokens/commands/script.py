"""Create printable sheets based on a script json file."""
# Standard Library
import argparse
import json
from pathlib import Path
import re
import sys
from zipfile import BadZipFile
import os.path

# Third Party
from jsonschema import validate, ValidationError
from rich import print
from rich.live import Live
from wand.exceptions import BlobError
from wand.image import Image

# Application Specific
from .. import data_dir
from .. import component_path as default_component_path
from ..helpers.printable import Printable
from ..helpers.progress_group import setup_progress_group
from ..helpers.token_components import TokenComponents
from ..helpers.token_creation import create_text_script_block
from ..helpers.script_utils import flatten_role_entries, get_script_meta, normalize_role_spacing
from ..helpers.logo_generator import generate_logo, ask_extra_description
from ..helpers.pdf_branding import brand_pdf, merge_flatten_and_optimize
from ..helpers.text_color import ask_name_color, resolve_name_color
from ..helpers.text_tools import format_filename
from ..helpers.botc_scripts_lookup import find_and_download_script

# Path to the bundled name font (used to overlay the script name onto the PDFs).
# Swap this out (or make it a CLI arg) if you want to support multiple fonts later.
NAME_FONT = data_dir / "fonts" / "LHF_Unlovable.ttf"

# Directory generated logos are cached in, keyed by script name, so re-running the same script
# doesn't re-generate (and re-pay for) the same logo.
LOGO_CACHE_DIR = Path("logos")

# Pixel-space placement of the name/logo overlays, relative to the --paper-width/--paper-height
# the sheets were generated with (default 2550x3300px). brand_pdf() scales these to whatever the
# actual PDF page size turns out to be, so they stay correct even if that differs slightly.
SCRIPT_NAME_BOX = {"xy": (1111, 40), "height": 300, "width": 1000}
SCRIPT_LOGO_BOX = {"xy": (400, 5), "width": 300, "height": 300}
BACKSIDE_NAME_BOX = {"xy": (666, 777), "width": 1111, "height": 666, "wrap": True}
BACKSIDE_LOGO_BOX = {"xy": (826, 2332), "width": 850, "height": 800}

# Character "type" folders (alongside demon/minion/outsider/townsfolk/traveller) whose
# scriptblocks get stacked on the backside rather than appearing on the front page.
SPECIAL_TYPE_FOLDERS = ("fabled", "loric")
# Character "type" folders whose characters are left off the script sheet entirely.
TRAVELLER_FOLDERS = ("traveller", "traveler")
# Icon placed in front of each Bootlegger rule on the backside (copy of the Loric Bootlegger icon).
BOOTLEGGER_ICON = data_dir / "components" / "BootleggerIcon.png"
# Backside: traveller icons stacked vertically at the right edge (pixels, paper space).
TRAVELLER_ICON_TOP = 150
TRAVELLER_ICON_RIGHT_MARGIN = 25
SPECIAL_TYPE_START = (700, 1500)


def _parse_args():
    parser = argparse.ArgumentParser(
        prog="botc_tokens script",
        description='Create printable script sheets based on a script json file.'
    )
    parser.add_argument('script', type=str,
                        help='the json file or directory containing the script info. If this is not an '
                             'existing file/directory, it is treated as a script name to search for and '
                             'download (latest version) from botcscripts.com instead.')
    parser.add_argument('--nightorder', type=str,
                        help='the json file containing the nightorder.')
    token_dir_default = 'tokens'
    parser.add_argument('--token-dir', type=str, default=token_dir_default,
                        help="Name of the directory in which to find the token images. Ignored if script is a "
                             f"directory. (Default: {token_dir_default})")
    parser.add_argument('--components', type=str, default=default_component_path,
                        help="The directory or zip in which to find the token components. (leaves, backgrounds, etc.)")
    parser.add_argument('-o', '--output-dir', type=str, default=None,
                        help="Name of the directory in which to output the sheets. "
                             "(Default: ./print)")
    margin_default = 74
    parser.add_argument('--margin-horizontal', type=int, default=margin_default,
                        help=f"The margin (in pixels) between the left/right edge of the paper and the tokens. "
                             f"(Default: {margin_default})")
    parser.add_argument('--margin-vertical', type=int, default=margin_default,
                        help=f"The margin (in pixels) between the top/bottom of the paper and the tokens. "
                             f"(Default: {margin_default})")
    paper_width_default = 2550
    parser.add_argument('--paper-width', type=int, default=paper_width_default,
                        help="The width (in pixels) of the paper to use for the tokens. "
                             f"(Default: {paper_width_default})")
    paper_height_default = 3300
    parser.add_argument('--paper-height', type=int, default=paper_height_default,
                        help="The height (in pixels) of the paper to use for the tokens. "
                             f"(Default: {paper_height_default})")
    parser.add_argument("--blocklinebreak", type=bool, default=False, help="Block type switch after type changes")
    parser.add_argument("--blankbg", action="store_true", help="create the pdf with a printer friendly blank background")
    parser.add_argument("--skip-branding", action="store_true",
                        help="Skip name/logo overlay + merge/optimize (only produce the raw script/backside PDFs).")
    parser.add_argument("--logo-description", type=str, default=None,
                        help="Extra description to feed into the logo prompt. If omitted, you'll be asked "
                             "interactively (unless --skip-branding is set).")
    parser.add_argument("--logo-provider", type=str, default="pollinations", choices=["pollinations", "openai"],
                        help="Which image-generation backend to use for the logo. 'pollinations' is free and "
                             "needs no API key. 'openai' is paid and needs OPENAI_API_KEY. (Default: pollinations)")
    parser.add_argument("--logo-reference-image", type=str, default=None,
                        help="Path to a local image to use as a style/composition reference for the logo. "
                             "Only supported with --logo-provider openai.")
    parser.add_argument("--name-color", type=str, default=None,
                        help="Hex color code (e.g. #FFFFFF) for the script-name text on both PDFs. If "
                             "omitted, you'll be asked interactively (unless --skip-branding is set). "
                             "Falls back to black if empty or invalid. Does not affect the role text "
                             "inside the scriptblocks.")
    args = parser.parse_args(sys.argv[2:])

    return args

def extract_role_ids(script):
    """Pull plain role-id strings out of a script list.

    Drops the leading "_meta" entry (if present). Custom/homebrew characters can appear as full
    objects (not just an id string) - their "id" field is used, and entries without one are
    dropped, since those aren't a role we can look up a token for.
    """
    script = list(script)
    if script and isinstance(script[0], dict):
        script.pop(0)
    role_ids = []
    for role in script:
        if isinstance(role, dict):
            role_id = role.get("id")
            if role_id:
                role_ids.append(role_id)
        elif role:
            role_ids.append(role)
    return role_ids

def create_order(script, order):
    role_ids = extract_role_ids(script)
    print(f"[green]script {set(role_ids)}...[/]")
    intersected_roles_night = list(set(role_ids) & set(order))
    sorted_night = sorted(intersected_roles_night, key=lambda x: order.index(x))
    return sorted_night

def find_special_type_scriptblocks(token_dir, script):
    """Find the -scriptblock.png files for any SPECIAL_TYPE_FOLDERS character (fabled, loric,
    ...) present in the script, in the script's own order."""
    role_ids = extract_role_ids(script)

    token_files = Path(token_dir).rglob("*.png")
    scriptblock_images = [
        f for f in find_scriptblocks(token_files)
        if any(part.lower() in SPECIAL_TYPE_FOLDERS for part in f.parts)
    ]

    matches = []
    for role_id in role_ids:
        role_name = role_id.lower().strip()
        role_file = next(
            (t for t in scriptblock_images
             if role_name == t.stem.lower().replace("'", "").replace("-scriptblock", "")),
            None)
        if role_file:
            matches.append(role_file)
    return matches

def filter_special_type_roles(script, role_images):
    """Drop any script entries of a SPECIAL_TYPE_FOLDERS type (fabled, loric, ...) - those are
    placed on the backside (see find_special_type_scriptblocks) instead of the front page, so
    they shouldn't also take up a slot there.
    """
    filtered = []
    for role in script:
        role_id = role.get("id") if isinstance(role, dict) else role
        if role_id:
            role_name = role_id.lower().strip()
            role_file = next(
                (t for t in role_images
                 if role_name == t.stem.lower().replace("'", "").replace("-scriptblock", "")),
                None)
            if role_file and any(part.lower() in SPECIAL_TYPE_FOLDERS for part in role_file.parts):
                continue
        filtered.append(role)
    return filtered

def run():
    """Create printable sheets based on a script json file."""
    args = _parse_args()

    if args.blankbg:
        print("blankbg is enabled")
        # code to enable the feature
    else:
        print("blankbg is disabled")
    # Read the script json file
    print(f"[green]Reading {args.script}...[/]")
    try:
        script = load_script(args)
        with open(data_dir / "nightsheet.json", "r") as f:
              nightorder = json.load(f)

    except RuntimeError as e:
        print(f"[red]Error:[/] Unable to load script {args.script}: {str(e)}")
        return 1

    script_name, script_author = get_script_meta(script)
    script, travellers = split_travellers(script, args.token_dir)
    if travellers:
        print(f"[green]Reisende kommen auf die Rueckseite statt nach vorne:[/] {', '.join(travellers)}")

    # Find all the token images
    token_files = Path(args.token_dir).rglob("*.png")
    print("[green]Finding Token Images...[/]")
    role_images = find_scriptblocks(token_files)

    # fabled/loric characters are placed on the backside (see create_backside), not the front page.
    script = filter_special_type_roles(script, role_images)

    # If there's an odd number of townsfolk and an even number of outsiders, insert a blank
    # role slot between the two blocks so the columns line up.
    script = normalize_role_spacing(script, role_images, get_role_type_by_name)

    # Create the printable sheets
    if args.output_dir is None:
        args.output_dir = "print"
    output_dir = Path(args.output_dir)
    print(f"[green]Creating sheets in {output_dir}...[/]", end="")
    output_dir.mkdir(parents=True, exist_ok=True)

    progress_group, overall_progress, step_progress = setup_progress_group()

    with Live(progress_group):
        overall_progress.add_task("Creating Sheets", total=None)
        step_task = step_progress.add_task("Adding roles")
        role_page = Printable(
            output_dir,
            basename="script",
            page_width=args.paper_width,
            page_height=args.paper_height,
            margin_vertical=args.margin_vertical,
            margin_horizontal=args.margin_horizontal
        )
        components = load_components(args.components)
        if components is None:
            print(f"\n[red]Error:[/][bold] Could not load component")
            return

        if args.blankbg :
            print("get_script_bg_blank")
            role_page.set_background(components.get_script_bg_blank())
        else:
            print("get_script_bg")
            role_page.set_background(components.get_script_bg())

        role_page.set_linebreak(components.get_script_type_line())
        role_page.set_linebreak_switch(components.get_script_type_line_broken())
        role_page.set_start(200 + role_page.margin_horizontal,320 + role_page.margin_vertical)

        process_tokens(role_images, role_page, script,
                       step_progress, step_task, args.blocklinebreak, components)

        # Save the last pages
        step_progress.update(step_task, description="Saving pages")
        role_page.write()

        # Clean up
        role_page.close()

    create_backside(nightorder, args)

    if not args.skip_branding:
        finalize_output(args, script_name)

    return None


def finalize_output(args, script_name):
    """Generate the logo, overlay name+logo onto both PDFs, then merge/flatten/optimize.

    Runs outside the Live progress display since it may prompt for interactive input and makes
    outbound API calls.
    """
    if not script_name:
        print("[yellow]Warning:[/] No script name found in the script's _meta entry; "
              "skipping name/logo branding and merge.")
        return

    output_dir = Path(args.output_dir)
    script_pdf = output_dir / "script.pdf"
    backside_pdf = output_dir / "backside.pdf"

    extra_description = args.logo_description
    if extra_description is None:
        extra_description = ask_extra_description()

    name_color_input = args.name_color
    if name_color_input is None:
        name_color_input = ask_name_color()
    name_color = resolve_name_color(name_color_input)

    # Cache generated logos by script name (in ./logos), so re-running the same script doesn't
    # re-generate (and, for the openai provider, re-pay for) the same logo.
    LOGO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    logo_path = LOGO_CACHE_DIR / f"{format_filename(script_name)}.png"

    if not os.path.isfile(logo_path):
        try:
            generate_logo(script_name, logo_path, extra_description=extra_description, provider=args.logo_provider,
                          reference_image=args.logo_reference_image)
        except (RuntimeError, ValueError) as e:
            print(f"[red]Error:[/] {str(e)}")
            print("[yellow]Skipping branding/merge - raw script.pdf and backside.pdf are still available.[/]")
            return
    else:
        print(f"[green]Nutze gecachtes Logo:[/] {logo_path}")

    print("[green]Overlaying name and logo onto script.pdf and backside.pdf...[/]")
    brand_pdf(script_pdf, script_name, NAME_FONT, logo_path,
              name_box=SCRIPT_NAME_BOX, logo_box=SCRIPT_LOGO_BOX,
              paper_width_px=args.paper_width, paper_height_px=args.paper_height,
              name_color=name_color)
    brand_pdf(backside_pdf, script_name, NAME_FONT, logo_path,
              name_box=BACKSIDE_NAME_BOX, logo_box=BACKSIDE_LOGO_BOX,
              paper_width_px=args.paper_width, paper_height_px=args.paper_height,
              name_color=name_color)

    final_path = output_dir / f"{format_filename(script_name)}.pdf"
    print(f"[green]Merging, flattening and optimizing -> {final_path}[/]")
    merge_flatten_and_optimize([script_pdf, backside_pdf], final_path)

    # The raw front/back PDFs are just intermediate artifacts once they're merged into final_path.
    script_pdf.unlink(missing_ok=True)
    backside_pdf.unlink(missing_ok=True)


def create_backside(nightorder, args):
    try:
        script = load_script(args)
    except RuntimeError as e:
        print(f"[red]Error:[/] Unable to load script {args.script}: {str(e)}")
        return 1

    # Travellers are not part of the night order; their icons get their own column (see below).
    script, travellers = split_travellers(script, args.token_dir)

    token_files = Path(args.token_dir).rglob("*.png")
    print("[green]Finding Token Images...[/]")
    role_images = find_images(token_files, "nightorder")
    traveller_icons = []
    for traveller in travellers:
        icon = next((t for t in role_images
                     if traveller == t.stem.lower().replace("'", "").replace("-nightorder", "")), None)
        if icon:
            traveller_icons.append(icon)
        else:
            print(f"[yellow]Warning:[/] No icon found for traveller {traveller}")

    # Create the printable sheets
    print(f"[green]Creating sheets in {args.output_dir}...[/]", end="")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    role_page = Printable(
        output_dir,
        basename="backside",
        page_width=args.paper_width,
        page_height=args.paper_height,
        margin_vertical=args.margin_vertical,
        margin_horizontal=args.margin_horizontal
    )
    components = load_components(args.components)
    if components is None:
        print(f"\n[red]Error:[/][bold] Could not load component")
        return

    if not args.blankbg:
        role_page.set_background(components.get_script_back())
    else:
        role_page.set_background(components.get_script_back_blank())

    if traveller_icons:
        # Keep the first-night row (top edge) clear of the traveller column on the right.
        role_page.night_row_right_edge = role_page.right_column_x(traveller_icons, TRAVELLER_ICON_RIGHT_MARGIN)

    othernight = nightorder["otherNight"].copy()
    othernight.reverse()

    role_page.set_start(450, 30)
    first_role_files = resolve_night_role_files(role_images, nightorder["firstNight"], script)
    scale_first = role_page.night_token_scale(first_role_files, True)

    # Note: role_page.page_height (not args.paper_height) - the printable area is already
    # reduced by the vertical margins, so using the full paper height here left almost no room
    # for the row and clipped it off the bottom of the page.
    role_page.set_start(args.paper_width - 450, role_page.page_height - 150)
    other_role_files = resolve_night_role_files(role_images, othernight, script)
    scale_other = role_page.night_token_scale(other_role_files, False)

    # Icons on both rows must always be the same size, and never larger than native size.
    night_scale = min(scale_first, scale_other, 1.0)

    role_page.set_start(450, 30)
    process_first_night_order(first_role_files, role_page, True, night_scale)

    role_page.set_start(args.paper_width - 450, role_page.page_height - 150)
    process_first_night_order(other_role_files, role_page, False, night_scale)

    special_type_files = find_special_type_scriptblocks(args.token_dir, script)
    # Bootlegger rules from the _meta entry: one block each (Bootlegger icon + rule text), right
    # below the fabled/loric blocks.
    bootlegger_blocks = create_bootlegger_blocks(get_bootlegger_rules(script), components)
    if bootlegger_blocks:
        # The rules replace the generic Bootlegger loric block - show it only when there are no rules.
        special_type_files = [f for f in special_type_files
                              if f.stem.lower().replace("-scriptblock", "") != "bootlegger"]
    if special_type_files or bootlegger_blocks:
        role_page.add_stacked_tokens(special_type_files + bootlegger_blocks, *SPECIAL_TYPE_START)
    for block in bootlegger_blocks:
        block.close()

    if traveller_icons:
        role_page.add_right_column(traveller_icons, top=TRAVELLER_ICON_TOP, right_margin=TRAVELLER_ICON_RIGHT_MARGIN)

    role_page.write()
    role_page.close()
    return None


def load_components(component_package):
    """Handle loading the components from a directory or zip file, and alerting the user if it fails."""
    try:
        components = TokenComponents(component_package)
    except BlobError as e:
        print(f"\n[red]Error:[/][bold] Could not load component: {str(e)}[/]")
        return None
    except BadZipFile:
        print(f"\n[red]Error:[/][bold] Could not load components from '{component_package}' it does not appear to be a "
              "valid components package.[/]")
        return None
    except FileNotFoundError as e:
        print(f"\n[red]Error:[/][bold] Unable to load components from '{component_package}': {str(e)}")
        return None
    return components

def resolve_night_role_files(role_images, nightorder, script):
    """Resolve a night-order role-id list to their nightorder token files, in order."""
    sorted_order = create_order(script.copy(), nightorder)

    role_files = []
    for role in sorted_order:
        role_name = role.lower().strip()
        role_file = next((t for t in role_images if role_name == t.stem.lower().replace("'", "").replace("-nightorder", "")), None)
        if not role_file:
            print(f"[yellow]Warning:[/] No token found for {role_name}")
            continue
        role_files.append(role_file)
    return role_files

def process_first_night_order(role_files, role_page, firstnight, scale):
    for role_file in role_files:
        role_page.add_night_token(role_file, firstnight, scale=scale)

def process_tokens(role_images, role_page, script,
                   step_progress, step_task, blockline, components):
    """Do all the processing.

    If we are being honest, this function exists separate from the run() function only to decrease its complexity.
    """
    last_character = "townsfolk"
    role_page.write_typeline(components, last_character)

    for i in range(len(script)):
        role = script[i]
        forceSwitch = False

        if isinstance(role, dict):
            continue  # Skip metadata

        if role == "":
            # Blank spacer (see normalize_role_spacing): keep the slot empty, no type change, no line.
            role_page.skip_slot()
            continue

        if i < (len(script)-1) and i > 1:
            last_role = script[i-1]
            if last_role != None and not isinstance(last_role, dict) and get_role_type_by_name(role, role_images) != get_role_type_by_name(last_role, role_images) and (i % 2) == 0:
                if(blockline == True):
                    forceSwitch = True

        role_name = role.lower().strip()

        step_progress.update(step_task, description=f"Adding {role_name.title()}")
        # See if we have tokens for this role
        role_file = next((t for t in role_images if role_name == t.stem.lower().replace("'", "").replace("-scriptblock", "")), None)
        if not role_file:
            continue
        # Check if we should add duplicate tokens
        character_type = get_character_type(str(role_file))

        if character_type != last_character:
            role_page.add_breakline(i)
            role_page.write_typeline(components, character_type)

        role_page.add_script_token(role_file, forceSwitch)
        last_character = character_type

def get_role_type_by_name(role, role_images):
    role_name = role.lower().strip()
    if not role_name:
        return None  # blank spacer - must not match the empty "-scriptblock.png" placeholder files
    role_file = next(
        (t for t in role_images if role_name == t.stem.lower().replace("'", "").replace("-scriptblock", "")), None)
    if not role_file:
        print(f"[yellow]Warning:[/] No token found for {role_name}")
        return
    return get_character_type(str(role_file))

def get_character_type(role_file):
    if "demon" in role_file:
        return "demon"
    elif "minion" in role_file:
        return "minion"
    elif "outsider" in role_file:
        return "outsider"
    elif "townsfolk" in role_file:
        return "townsfolk"

    return ""

def find_scriptblocks(token_files):
    return find_images(token_files, "scriptblock")

def find_images(token_files, needle):
    """Populate role and reminder lists with the images we find."""
    role_images = []
    for img_file in token_files:

        if needle in img_file.name:
            role_images.append(img_file)
    return role_images


def load_script(args):
    """Load the script from the file or directory, or - if that path doesn't exist - treat it as
    a script name to search for and download from botcscripts.com."""
    script_path = Path(args.script)
    if not script_path.exists():
        downloaded_path = find_and_download_script(args.script)
        args.script = str(downloaded_path)  # Cache: the 2nd load_script() call in this run reuses this.
        script_path = downloaded_path

    script = []
    if script_path.is_dir():
        script = [file.stem for file in script_path.rglob("*.png") if "Reminder" not in file.name]
        args.token_dir = str(script_path)
    else:
        with open(args.script, "r") as f:
            content = f.read()
        try:
            script = json.loads(content)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"'{args.script}' is not valid JSON (line {e.lineno}, column {e.colno}): {e.msg}"
            ) from e
    return flatten_role_entries(script)


def get_bootlegger_rules(script):
    """The Bootlegger rules from the script's _meta entry ("bootlegger": [...]), as non-empty strings."""
    meta = script[0] if script and isinstance(script[0], dict) else {}
    rules = meta.get("bootlegger") or []
    if isinstance(rules, str):
        rules = [rules]
    return [r.strip() for r in rules if isinstance(r, str) and r.strip()] if isinstance(rules, list) else []


def create_bootlegger_blocks(rules, components):
    """One script block per Bootlegger rule, with the Bootlegger icon in front of the rule text."""
    blocks = []
    for rule in rules:
        icon = Image(filename=str(BOOTLEGGER_ICON))
        blocks.append(create_text_script_block(icon, rule, components))
    if blocks:
        print(f"[green]Bootlegger-Regeln auf der Rueckseite:[/] {len(blocks)}")
    return blocks


def split_travellers(script, token_dir):
    """Separate traveller characters from the rest of the script.

    Travellers are left off the front page and the night order; their icons are lined up on the
    backside instead (see create_backside). Returns (script_without_travellers, traveller_ids).
    """
    traveller_ids = {
        f.stem.lower().replace("'", "").replace("-scriptblock", "")
        for f in Path(token_dir).rglob("*-scriptblock.png")
        if any(part.lower() in TRAVELLER_FOLDERS for part in f.parts)
    }
    traveller_ids.discard("")
    kept, travellers = [], []
    for role in script:
        if isinstance(role, str) and role.lower().strip() in traveller_ids:
            travellers.append(role.lower().strip())
        else:
            kept.append(role)
    return kept, travellers