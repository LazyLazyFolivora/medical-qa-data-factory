"""Stable model-facing material handles; storage retains canonical identifiers."""


def material_aliases(snapshot, steps):
    aliases = {}
    for step in reversed(steps):
        saved = step.get("parameters", {}).get("material_aliases")
        if saved is not None:
            aliases = dict(saved)
            break
    known = set(aliases.values())
    next_number = max((int(k[1:]) for k in aliases), default=0) + 1
    for aid in sorted(snapshot.artifacts):
        if snapshot.artifacts[aid].artifact_type == "evidence_material" and aid not in known:
            aliases[f"M{next_number:04d}"] = aid
            next_number += 1
    return aliases


def translate_references(value, mapping, field=""):
    """Translate declared reference fields, never query text or source quotations."""
    if isinstance(value, str):
        reference = field in {"id", "materials_without_extracted_claims"} or field.endswith("_id") or field.endswith("_ids")
        return mapping.get(value, value) if reference else value
    if isinstance(value, list):
        return [translate_references(v, mapping, field) for v in value]
    if isinstance(value, dict):
        return {(mapping.get(k, k) if field == "source_sections" else k): translate_references(v, mapping, k)
                for k, v in value.items() if k != "material_aliases"}
    return value
