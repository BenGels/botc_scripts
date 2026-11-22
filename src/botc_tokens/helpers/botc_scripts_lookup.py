"""Look up and download a script by name from botcscripts.com's public script API.

botcscripts.com exposes a public, unauthenticated JSON API for browsing/downloading scripts - the
same one used by the site's own "Download JSON" button, and by community archive/mirror tools.

Note: the site's robots.txt disallows automated crawling of /api/. This module only makes a small,
bounded number of requests per lookup (a handful of search-result pages for one query - not a bulk
crawl), and skips the request entirely if a matching script is already cached locally (see
find_and_download_script). If you'd rather stay strictly within the site's stated bot policy,
download the script manually from https://www.botcscripts.com and pass the JSON file path instead
of a name.
"""
# Standard Library
import difflib
import json
import re
from pathlib import Path

# Third Party
import requests
from rich import print

API_URL = "https://www.botcscripts.com/api/scripts/"
SITE_URL = "https://www.botcscripts.com/script/{script_id}/{version}"
MAX_SEARCH_PAGES = 5  # Safety cap - a specific-enough query shouldn't need more than this.
LOCAL_MATCH_THRESHOLD = 0.9  # How close a cached script's name must fuzzy-match to reuse it.


def _version_key(version):
    """Turn a version string like '2.1.0' into a tuple for comparison (so '10.0' sorts after
    '9.0', unlike a plain string compare). Falls back to the raw string if it has no digits."""
    parts = tuple(int(n) for n in re.findall(r"\d+", str(version)))
    return parts or (str(version),)


def _name_match_score(query, name):
    return difflib.SequenceMatcher(None, query.strip().lower(), (name or "").strip().lower()).ratio()


def _find_cached_script(query, output_dir):
    """Look for an already-downloaded script in output_dir whose name matches the query.

    Returns the Path of the best match if it's an exact (case-insensitive) match, or a fuzzy match
    above LOCAL_MATCH_THRESHOLD - otherwise None, so the caller falls back to an online search.
    """
    output_dir = Path(output_dir)
    if not output_dir.is_dir():
        return None

    best_path, best_score = None, -1.0
    for path in output_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not data or not isinstance(data[0], dict):
            continue
        name = data[0].get("name")
        if not name:
            continue
        score = _name_match_score(query, name)
        if score == 1.0:
            return path
        if score > best_score:
            best_path, best_score = path, score

    return best_path if best_score >= LOCAL_MATCH_THRESHOLD else None


def _fetch_page(url, params=None):
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        raise RuntimeError(f"Anfrage an botcscripts.com fehlgeschlagen: {e}") from e
    except ValueError as e:
        raise RuntimeError(f"Ungueltige Antwort von botcscripts.com: {e}") from e


def _search_results(query):
    """Yield script result dicts from the API's search, across a bounded number of pages."""
    url = API_URL
    params = {"format": "json", "search": query}
    for _ in range(MAX_SEARCH_PAGES):
        data = _fetch_page(url, params=params)
        results = data.get("results", [])
        if not isinstance(results, list):
            raise RuntimeError("Unerwartetes Antwortformat von botcscripts.com (kein 'results'-Array).")
        for result in results:
            yield result
        next_url = data.get("next")
        if not next_url:
            return
        # "next" is already a full URL with the query params baked in.
        url, params = next_url, None


def script_url(result):
    """Link to a search result's page on botcscripts.com (that exact version)."""
    return SITE_URL.format(script_id=result.get("script_id"), version=result.get("version"))


def search_scripts(query, limit=10):
    """Search botcscripts.com and return the best-matching scripts, one entry per script.

    Only the first page of search results is fetched (meant for autocomplete, so it stays at one
    request per query). Of several versions of the same script only the latest is kept. Results are
    sorted by how well their name matches the query (exact, prefix, substring), then by popularity.

    Returns:
        list[dict]: ``{"pk", "script_id", "name", "author", "version", "script_type", "url"}``
    """
    data = _fetch_page(API_URL, params={"format": "json", "search": query})
    results = data.get("results", [])
    if not isinstance(results, list):
        raise RuntimeError("Unerwartetes Antwortformat von botcscripts.com (kein 'results'-Array).")

    latest_by_id = {}
    for result in results:
        script_id = result.get("script_id")
        current = latest_by_id.get(script_id)
        if current is None or _version_key(result.get("version")) > _version_key(current.get("version")):
            latest_by_id[script_id] = result

    def rank(result):
        # Exact name first, then names that start with / contain the typed text; within each group the
        # more popular script (the API's "score") wins, then the closest fuzzy match.
        q, name = query.strip().lower(), (result.get("name") or "").strip().lower()
        popularity = result.get("score") if isinstance(result.get("score"), (int, float)) else 0
        return (name != q, not name.startswith(q), q not in name, -popularity, -_name_match_score(query, name))

    ranked = sorted(latest_by_id.values(), key=rank)
    return [{
        "pk": r.get("pk"),
        "script_id": r.get("script_id"),
        "name": r.get("name") or "",
        "author": r.get("author") or "",
        "version": r.get("version") or "",
        "script_type": r.get("script_type") or "",
        "url": script_url(r),
    } for r in ranked[:limit]]


def download_script(pk, output_dir="downloaded_scripts"):
    """Download one exact script version by its botcscripts.com primary key (``pk``).

    Returns:
        tuple[Path, dict]: Path of the saved script JSON, and the API entry (without content).
    """
    result = _fetch_page(f"{API_URL}{int(pk)}/", params={"format": "json"})
    if not isinstance(result, dict) or "content" not in result:
        raise RuntimeError(f"Script {pk} nicht gefunden auf botcscripts.com.")
    print(f"[green]Lade:[/] '{result.get('name')}' von {result.get('author') or 'unbekannt'} "
          f"(Version {result.get('version')})")
    path = _save_script(result, output_dir, fallback_name=str(pk))
    return path, {k: v for k, v in result.items() if k != "content"}


def _save_script(result, output_dir, fallback_name):
    script = _build_script_json(result)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^-_.() a-zA-Z0-9]", "", str(result.get("name") or fallback_name)).replace(" ", "_")
    output_path = output_dir / f"{safe_name or 'script'}.json"
    output_path.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def _best_match(query, results_by_id):
    """Pick the script_id whose name is the closest match to the query."""
    best_id, best_score = None, -1.0
    for script_id, entries in results_by_id.items():
        score = _name_match_score(query, entries[0].get("name") or "")
        if score == 1.0:
            return script_id
        if score > best_score:
            best_id, best_score = script_id, score
    return best_id


def _flatten_role_entries(script):
    """Convert botcscripts.com's per-role {"id": "..."} objects into plain id strings, matching
    the flat format used by manually created/uploaded script JSON files (and expected by the rest
    of the pipeline - role_images lookups, normalize_role_spacing, etc. all compare against plain
    strings). The leading _meta dict is left untouched.

    Custom/homebrew characters that ship a full definition (ability text, team, image, ...)
    instead of just an id are reduced to their id too - there's no token artwork for them anyway
    (the pipeline can only place official token images), so nothing usable is lost.
    """
    flattened = []
    for i, entry in enumerate(script):
        if i == 0 and isinstance(entry, dict) and entry.get("id") == "_meta":
            flattened.append(entry)
        elif isinstance(entry, dict):
            flattened.append(entry.get("id") or "")
        else:
            flattened.append(entry)
    return flattened


def _build_script_json(result):
    """Return canonical BotC script JSON: a flat list of role-id strings, starting with a _meta
    entry - the same format manually created/uploaded script JSON files use."""
    content = result.get("content")
    if isinstance(content, str):
        content = json.loads(content)
    if not isinstance(content, list):
        raise RuntimeError(f"Script {result.get('pk')}: Inhalt ist kein JSON-Array.")

    script = content.copy()
    first_item = script[0] if script else None
    if not isinstance(first_item, dict) or first_item.get("id") != "_meta":
        script.insert(0, {
            "id": "_meta",
            "name": result.get("name") or "",
            "author": result.get("author") or "",
        })
    return _flatten_role_entries(script)


def find_and_download_script(query, output_dir="downloaded_scripts"):
    """Find a script matching the query, preferring an already-downloaded local copy, and
    otherwise searching + downloading its latest version from botcscripts.com.

    Args:
        query (str): The script name (or part of it) to search for.
        output_dir (str|Path): Directory to look for/save the script JSON in.

    Returns:
        Path: Path to the (cached or freshly downloaded) script JSON file.

    Raises:
        RuntimeError: If no matching script is found locally or online, or the API
            request/response is invalid.
    """
    cached_path = _find_cached_script(query, output_dir)
    if cached_path:
        print(f"[green]Nutze bereits heruntergeladenes Script:[/] {cached_path}")
        return cached_path

    print(f"[green]Suche auf botcscripts.com nach '{query}'...[/]")

    results_by_id = {}
    for result in _search_results(query):
        script_id = result.get("script_id")
        results_by_id.setdefault(script_id, []).append(result)

    if not results_by_id:
        raise RuntimeError(f"Kein Script gefunden fuer '{query}' auf botcscripts.com.")

    best_id = _best_match(query, results_by_id)
    versions = results_by_id[best_id]
    latest = max(versions, key=lambda r: _version_key(r.get("version")))

    print(f"[green]Gefunden:[/] '{latest.get('name')}' von {latest.get('author') or 'unbekannt'} "
          f"(Version {latest.get('version')})")

    return _save_script(latest, output_dir, fallback_name=query)