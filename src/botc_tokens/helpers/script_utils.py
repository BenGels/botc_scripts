"""Helper functions for working with the parsed script list (meta info, role-type spacing)."""


def get_script_meta(script):
    """Extract the ``_meta`` entry (if any) from a loaded script.

    Args:
        script (list): The loaded script, as returned by ``load_script``. May start with a
            ``{"id": "_meta", "name": ..., "author": ...}`` dict.

    Returns:
        tuple: ``(name, author)``. Either may be ``None`` if not present.
    """
    if script and isinstance(script[0], dict):
        meta = script[0]
        return meta.get("name"), meta.get("author")
    return None, None


def flatten_role_entries(script):
    """Turn role objects like ``{"id": "knight"}`` into plain id strings (``"knight"``).

    The official script tool and botcscripts.com both export roles as objects; the rest of the
    pipeline compares plain strings (objects would otherwise be skipped as if they were metadata,
    leaving the front page empty). The ``_meta`` entry stays a dict, entries without an id are dropped.
    """
    flattened = []
    for i, entry in enumerate(script):
        if isinstance(entry, dict):
            if entry.get("id") == "_meta" or (i == 0 and "id" not in entry):  # Metadaten bleiben
                flattened.append(entry)
            elif isinstance(entry.get("id"), str) and entry["id"].strip():
                flattened.append(entry["id"])
        else:
            flattened.append(entry)
    return flattened


def normalize_role_spacing(script, role_images, get_role_type_by_name):
    """Insert a blank role slot ("") between the last townsfolk and first outsider.

    Blood on the Clocktower script sheets look best when the townsfolk/outsider blocks are
    visually separated. If a script has an odd number of townsfolk and an even number of
    outsiders, the columns would misalign, so we insert one blank slot right after the last
    townsfolk entry (and before the first outsider entry) to fix the alignment - mirroring what
    script authors already do by hand in several example scripts.

    Args:
        script (list): The loaded script (may include a leading ``_meta`` dict).
        role_images (list[Path]): Token image files, used to classify each role's type.
        get_role_type_by_name (callable): ``(role_name, role_images) -> type_str`` classifier.

    Returns:
        list: The script, with a "" inserted if the imbalance condition is met. If a "" is
            already present between the two blocks, the script is returned unchanged.
    """
    types = []
    for role in script:
        if isinstance(role, dict) or role == "":
            types.append(None)
        else:
            types.append(get_role_type_by_name(role, role_images))

    townsfolk_count = types.count("townsfolk")
    outsider_count = types.count("outsider")

    if townsfolk_count % 2 != 1 or outsider_count % 2 != 0:
        return script

    townsfolk_indices = [i for i, t in enumerate(types) if t == "townsfolk"]
    if not townsfolk_indices:
        return script
    last_townsfolk_idx = max(townsfolk_indices)

    # If there's already a blank slot right after the last townsfolk, leave the script alone.
    if last_townsfolk_idx + 1 < len(script) and script[last_townsfolk_idx + 1] == "":
        return script

    new_script = list(script)
    new_script.insert(last_townsfolk_idx + 1, "")
    return new_script
