# BotC Scripts – Token & Script-PDF Tools for Blood on the Clocktower
A fan-made toolkit to create custom tokens **and print-ready, double-sided script sheets** for the game
Blood on the Clocktower – usable from the command line, as a self-hosted web app, via a small REST API and
as an MCP connector for Claude.

> This project is based on [**Tsubashi/botc_tokens**](https://github.com/Tsubashi/botc_tokens) by Tsubashi.
> All credit for the original token tooling goes to the upstream authors. See [Origin & history](#origin--history).

![An example of tokens made by this utility](https://github.com/Tsubashi/botc_tokens-Examples/blob/31b118fc96e0f2aaa6b20d7547f2923a6c73ff34/example.jpg?raw=true)

## Features
- `update` / `create` / `group`: download role data, render tokens, group them on printable sheets (upstream features)
- `update --bloodstar-url`: import custom characters from a [bloodstar](https://bloodstar.xyz) script
- `script`: double-sided script PDF – front: all roles with abilities, back: first/other night order
- Script lookup by name on [botcscripts.com](https://www.botcscripts.com) (cached in `downloaded_scripts/`)
- Branding: script name and a generated logo (Pollinations for free, or OpenAI) are put on both pages, then the
  PDF is merged, flattened and compressed
- German/English role texts (`inputs_DEEN`, helper scripts `updatenames.sh`, `updateability.sh`)
- Flask web app with user accounts, admin approval, OpenAI quota, REST API (`/api/v1`), OAuth and an MCP endpoint
- Docker image / `docker compose` setup

## Installation
To use this tool, you will need to have Python 3 installed on your computer. You can download it from the
[official website](https://www.python.org/downloads/).

Once you have Python installed, clone this repository and run the following command in the root folder
(`pip install botc_tokens` from PyPI installs the upstream version without the script/web features):
```bash
pip install .
```

We use [Wand](https://docs.wand-py.org/en/0.6.13/) to handle image manipulation, which requires the [ImageMagick](https://imagemagick.org/index.php) library to be installed on your 
system. Check the [wand install guide](https://docs.wand-py.org/en/0.6.13/guide/install.html) for details on how to get that set up.

**Note for Apple Silicon:** When running on Apple Silicon, Wand will not auto-detect the ImageMagick installation. 
You will need to set the `MAGICK_HOME` environment variable to your homebrew root, like so:
```bash
export MAGICK_HOME=/opt/homebrew/
```

The `script` command's final PDF merge also shells out to [Ghostscript](https://www.ghostscript.com/) (the `gs`
binary) to recompress the rasterized pages as JPEG, which shrinks the final PDF by roughly an order of magnitude.
If `gs` isn't found on your `PATH`, this step is skipped with a warning and you still get an (uncompressed) PDF.

## Usage
**For a full walkthrough and for more component packages, see the 
[example repository](https://github.com/Tsubashi/botc_tokens-Examples).**

The main script is run through its entrypoint `botc_tokens`. For example, you might use the `update` command to download
the latest assets from the web, and then the `create` command to generate the tokens, followed by a `group` command to 
organize them into a single printable sheet using a json file from the [official script tool](https://script.bloodontheclocktower.com/). 
You can use the `--help` flag to see the available commands and options.

### Updating from the web
If you want to create experimental tokens for your in-person games, you can use the `update` command to download the
current list from the [official wiki](https://wiki.bloodontheclocktower.com/). This is an incremental update, so it 
will only download assets that are not already present in the local folder. We highly recommend double-checking the
resulting entries, especially the reminder token section. While the utility does its best to guess, it isn't perfect.
If you already know the reminder tokens, you can create a JSON file for the utility to use. See the 
[example repository](https://github.com/Tsubashi/botc_tokens-Examples) for an example of the format.

**Please note:** recent changes in the script tool crashed wiki
download, so we recommend using the `--use-built-in` option in order generate base game roles as of date 2025-11.
```bash
botc_tokens update --use-built-in --output-dir /path/to/inputs --reminders /path/to/reminders.json
```

### Updating from bloodstar url
If you have an already existing bloodstar script and you want to create tokens for your in-person games, you can use
the `update` command with `--bloodstar-url` option to download your characters in the tool's format.
The tool tries to convert the tokens as per our best knowledge. Please keep in mind, that the results are best
when the bloodstar script is filled with setup, first/other nights reminder text and reminder tokens.
Visit [bloodstar](https://bloodstar.xyz) to start your editing there.
```bash
botc_tokens update --output-dir /path/to/inputs --bloodstar-url https://www.bloodstar.xyz/p/TheBeardedDan/Krampus/script.json?9712244e
```

### Creating tokens
Once you have your role info, whether from the `update` command or from your own creation, you can use the `create`
command to generate the tokens. This will create a folder with the role name and a subfolder for each token type. If you
would like to customize the appearance of the tokens, you can use the `--component-dir` option to specify where to
find your custom components. Check the [example repository](https://github.com/Tsubashi/botc_tokens-Examples) 
for an example of the expected structure.
```bash
botc_tokens create /path/to/inputs -o /path/to/tokens --components /path/to/components
```

### Grouping tokens
Once you have created your token images, you can use the `group` command to organize the tokens into a single 
printable sheet. This command can take a directory or a JSON file with desired role grouping, which follows the
formatting of the [official script tool](https://script.bloodontheclocktower.com/).

```bash
botc_tokens group /path/to/script.json --token-dir /path/to/tokens --output-dir /path/to/printables
```

## And that's it!
You can now print your tokens and use them in your games. Remember, this is a fan-made utility and is not
affiliated with the official game in any way. We do not own the rights to the game, and we do not claim to. We are just
fans who want to make the game more accessible to everyone. Please support the creators by 
[buying the game](https://bloodontheclocktower.com/buy). It is absolutely worth it!

## Creating scripts
For those of you loving to run custom scripts, but want to look them a bit more fancy than what the official script tool
is giving you, you can create script sheets. The required objects will be created by running the `create` command, which
create also images with suffix -nightorder and -scriptblock

### Create script PDF from json script definiton
Once you have created your token images, you can use the `script` command to create a double sided script with all roles
and their description on the one side and the backside will have the role icons in order of the first night order on the
top and for all other nights on the botton of the script. This command can take a directory or a JSON file with desired role grouping, which follows the
formatting of the [official script tool](https://script.bloodontheclocktower.com/).

Instead of a JSON file you can also pass a script **name**; it is looked up on botcscripts.com and the newest version
is downloaded to `downloaded_scripts/`.

```bash
botc_tokens script /path/to/script.json --token-dir /path/to/tokens --output-dir /path/to/printables
# by name, A4 without margins, with branding and no interactive questions:
botc_tokens script "Trouble Brewing" --token-dir tokens_DEEN -o print \
  --paper-width 2480 --paper-height 3508 --margin-horizontal 0 --margin-vertical 0 \
  --logo-description "a bubbling cauldron" --name-color "#000000"
```

Useful options: `--nightorder` (custom night order JSON), `--blankbg` (printer friendly background),
`--skip-branding` (only the raw `script.pdf` / `backside.pdf`), `--logo-provider pollinations|openai`,
`--logo-reference-image` (OpenAI only), `--name-color`. Without `--logo-description` and `--name-color` the
command asks interactively. Generated logos are cached in `logos/<script name>.png`; delete the file to get a new one.
`make_script_pdf.sh` wraps this for fully non-interactive use (cron, CI).

## Setup

### Secrets / configuration
No keys or credentials are part of this repository. Copy the template and fill in what you need:
```bash
cp .env.example .env      # OPENAI_API_KEY=... (optional, only for OpenAI logos)
```
The following files contain secrets and are git-ignored (and excluded from the Docker image via `.dockerignore`):

| File | Content |
|---|---|
| `.env` | `OPENAI_API_KEY` |
| `webapp_data/` | user database (password hashes) and the Flask session secret (`secret_key`) – created on first start |
| `~/.config/botc-pdf/netrc` | Basic-auth login for `botc_pdf_api.sh` (outside the repo, `chmod 600`) |
| `.claude/settings.local.json` | local Claude Code permissions |

### Local (CLI)
```bash
python3 -m venv .venv
.venv/bin/pip install -e .            # needs ImageMagick (Wand) and optionally Ghostscript, see above
.venv/bin/botc_tokens update --use-built-in --output-dir inputs_DEEN
.venv/bin/botc_tokens create inputs_DEEN -o tokens_DEEN
```
Token input/output folders (`inputs_*`, `tokens_*`) are generated data and therefore not part of the repository.

### Web app
```bash
.venv/bin/pip install -r webapp/requirements.txt
.venv/bin/python -m webapp.app        # http://127.0.0.1:8090
```
First login: `admin` / `deleteme!` – changing the password is enforced. New registrations have to be approved
under "Benutzerverwaltung". All settings (`BOTC_WEB_*`, `BOTC_TOKEN_DIR`, ...) are documented in
[webapp/README.md](webapp/README.md) (German).

### Docker
The image bundles `botc_tokens`, the web app, the token images from `tokens_DEEN` and the cached logos, so create
the tokens first (see above), then:
```bash
docker compose up -d --build          # reads OPENAI_API_KEY from .env
```
Set `BOTC_WEB_PUBLIC_URL`, `BOTC_WEB_TRUSTED_PROXIES`, `BOTC_WEB_SECURE_COOKIES=1` and `BOTC_WEB_TRUST_PROXY=1`
for your own deployment behind an HTTPS reverse proxy (e.g. `BOTC_WEB_PUBLIC_URL=https://botc.example.com`,
`BOTC_WEB_TRUSTED_PROXIES=192.0.2.10`; both can go into `.env`). Data persists in the volumes `botc_data`, `botc_logos` and `botc_scripts`.

### API client & Claude integration
- `botc_pdf_api.sh <name|script.json>` creates a PDF through the web app's REST API (`BOTC_API_BASE`, `BOTC_NETRC`).
- The web app exposes an MCP endpoint at `<public url>/mcp` that can be added as a custom connector in claude.ai.
- `skills/botc-script-pdf` and `.claude/skills/` contain Claude skills that drive the CLI.

## Origin & history
This repository did not start from scratch. Its history in short:

1. **Upstream:** [Tsubashi/botc_tokens](https://github.com/Tsubashi/botc_tokens) – a Python CLI (`update`, `create`,
   `group`) that downloads role data from the official wiki and renders printable character and reminder tokens
   with ImageMagick/Wand. Examples and components live in
   [Tsubashi/botc_tokens-Examples](https://github.com/Tsubashi/botc_tokens-Examples).
2. **Fork:** [BenGels/botc_tokens](https://github.com/BenGels/botc_tokens) – the upstream code was forked to print
   tokens for in-person games in German. It got German/English role data (`inputs_DEEN`) with small shell helpers
   to merge translated names and abilities, a `--use-built-in` role set (after changes to the official script tool
   broke the wiki download), the bloodstar import and the first version of the `script` command, which renders the
   role list and the night order as a two-page script sheet. At that time the script name and logo still had to be
   added by hand in a PDF editor.
3. **Script automation:** To get rid of that manual step, scripts can now be fetched by name from botcscripts.com,
   a themed logo is generated (free via Pollinations, optionally OpenAI, background removed locally with `rembg`),
   name and logo are overlaid on both pages, and the result is merged and compressed with Ghostscript into a single
   print-ready PDF. Wrapper scripts make this usable without any interactive questions.
4. **Web app & integrations:** So that other players of the local group can create sheets without a Python setup,
   a Flask web app with accounts, admin approval and a weekly OpenAI quota was added, then a REST API, OAuth and an
   MCP endpoint so the PDFs can also be requested directly from Claude. Finally everything was packaged as a Docker
   image. Large parts of these later steps were developed with the help of
   [Claude Code](https://claude.com/claude-code) (see `CLAUDE.md` and the skills folders).
5. **This repository** collects that state as its own project; secrets were stripped and are configured via `.env`.

Thanks to Tsubashi and all contributors of botc_tokens – without the original project this one would not exist.
The upstream project is declared as MIT licensed (see `pyproject.toml`).
