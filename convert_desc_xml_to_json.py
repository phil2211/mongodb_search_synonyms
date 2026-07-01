#!/usr/bin/env python3
"""Convert MeSH DescriptorRecordSet XML (e.g. desc2026.xml) to JSON for MongoDB import."""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

DATE_FIELD_NAMES = frozenset({"LastUpdated", "DateIntroduced", "DateCreated"})
DATE_PART_KEYS = frozenset({"Year", "Month", "Day"})


def _normalize_attrib(attrib: dict[str, str]) -> dict[str, str]:
    return {key.strip(): value for key, value in attrib.items()}


def element_to_dict(element: ET.Element) -> Any:
    """Recursively convert an XML element to a JSON-friendly structure."""
    children = list(element)

    result: dict[str, Any] = {}
    for key, value in _normalize_attrib(element.attrib).items():
        result[key] = value

    child_groups: dict[str, list[Any]] = {}
    for child in children:
        child_value = element_to_dict(child)
        child_groups.setdefault(child.tag, []).append(child_value)

    for tag, values in child_groups.items():
        result[tag] = values[0] if len(values) == 1 else values

    text = (element.text or "").strip()
    if text:
        if result:
            result["text"] = text
        else:
            return text

    if not result:
        return None

    return result


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _is_date_parts(value: dict[str, Any]) -> bool:
    return DATE_PART_KEYS.issubset(value.keys())


def _date_parts_to_extended_json(value: dict[str, Any]) -> dict[str, str]:
    year = int(value["Year"])
    month = int(value["Month"])
    day = int(value["Day"])
    return {"$date": f"{year:04d}-{month:02d}-{day:02d}T00:00:00.000Z"}


def normalize_value(value: Any, *, field_name: str | None = None) -> Any:
    """Flatten type wrappers and convert date parts to MongoDB extended JSON."""
    if isinstance(value, dict):
        if field_name in DATE_FIELD_NAMES and _is_date_parts(value):
            return _date_parts_to_extended_json(value)

        if set(value.keys()) == {"String"}:
            return value["String"]

        if set(value.keys()) == {"text"}:
            return value["text"]

        return {key: normalize_value(item, field_name=key) for key, item in value.items()}

    if isinstance(value, list):
        return [normalize_value(item) for item in value]

    return value


def normalize_document(document: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_value(document)
    if not isinstance(normalized, dict):
        return {"value": normalized}
    return normalized


def _dump_extended_json(document: dict[str, Any]) -> str:
    """Serialize a document using MongoDB extended JSON conventions."""
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"))


def _extract_tree_numbers(document: dict[str, Any]) -> list[str]:
    tree_number_list = document.get("TreeNumberList")
    if not isinstance(tree_number_list, dict):
        return []
    return [tree for tree in _as_list(tree_number_list.get("TreeNumber")) if tree]


def _parent_tree_number(tree_number: str) -> str | None:
    if "." not in tree_number:
        return None
    return tree_number.rsplit(".", 1)[0]


def _extract_descriptor_uis_from_refs(
    container: Any,
    item_key: str,
) -> list[str]:
    if not isinstance(container, dict):
        return []

    uis: list[str] = []
    for item in _as_list(container.get(item_key)):
        if not isinstance(item, dict):
            continue
        ref = item.get("DescriptorReferredTo")
        if isinstance(ref, dict):
            ui = ref.get("DescriptorUI")
            if ui:
                uis.append(ui)
    return uis


def _extract_see_also(document: dict[str, Any]) -> list[str]:
    return _extract_descriptor_uis_from_refs(
        document.get("SeeRelatedList"),
        "SeeRelatedDescriptor",
    )


def _build_hierarchy_maps(
    ui_to_trees: dict[str, list[str]],
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    tree_to_ui: dict[str, str] = {}
    for ui, trees in ui_to_trees.items():
        for tree in trees:
            tree_to_ui[tree] = ui

    parents_map: dict[str, list[str]] = {}
    for ui, trees in ui_to_trees.items():
        parents = {
            tree_to_ui[parent_tree]
            for tree in trees
            if (parent_tree := _parent_tree_number(tree)) is not None
            and parent_tree in tree_to_ui
        }
        parents_map[ui] = sorted(parents)

    children_map: dict[str, set[str]] = {ui: set() for ui in ui_to_trees}
    for ui, parents in parents_map.items():
        for parent_ui in parents:
            children_map[parent_ui].add(ui)

    return parents_map, {ui: sorted(children) for ui, children in children_map.items()}


def _scan_ui_to_trees(input_path: Path, *, limit: int | None = None) -> dict[str, list[str]]:
    ui_to_trees: dict[str, list[str]] = {}
    record_count = 0

    with input_path.open("rb") as xml_file:
        for _event, element in ET.iterparse(xml_file, events=("end",)):
            if element.tag != "DescriptorRecord":
                continue

            document = element_to_dict(element)
            if isinstance(document, dict):
                ui = document.get("DescriptorUI")
                if ui:
                    ui_to_trees[ui] = _extract_tree_numbers(document)

            record_count += 1
            element.clear()

            if limit is not None and record_count >= limit:
                break

    return ui_to_trees


def _enrich_document(
    document: dict[str, Any],
    *,
    language_code: str | None,
    parents_map: dict[str, list[str]],
    children_map: dict[str, list[str]],
) -> dict[str, Any]:
    ui = document.get("DescriptorUI")
    tree_numbers = _extract_tree_numbers(document)

    document["treeNumbers"] = tree_numbers
    if ui:
        document["parentDescriptorUIs"] = parents_map.get(ui, [])
        document["childDescriptorUIs"] = children_map.get(ui, [])

    see_also = _extract_see_also(document)
    if see_also:
        document["seeAlso"] = see_also

    if language_code is not None:
        document["LanguageCode"] = language_code

    return document


def convert_xml_to_jsonl(
    input_path: Path,
    output_path: Path,
    *,
    limit: int | None = None,
) -> int:
    """Write one MongoDB document per line (JSONL / NDJSON) in extended JSON."""
    ui_to_trees = _scan_ui_to_trees(input_path, limit=limit)
    parents_map, children_map = _build_hierarchy_maps(ui_to_trees)

    language_code: str | None = None
    record_count = 0

    with input_path.open("rb") as xml_file, output_path.open("w", encoding="utf-8") as out:
        for event, element in ET.iterparse(xml_file, events=("start", "end")):
            if event == "start" and element.tag == "DescriptorRecordSet":
                language_code = _normalize_attrib(element.attrib).get("LanguageCode")

            elif event == "end" and element.tag == "DescriptorRecord":
                document = element_to_dict(element)
                if not isinstance(document, dict):
                    document = {"value": document}

                document = _enrich_document(
                    document,
                    language_code=language_code,
                    parents_map=parents_map,
                    children_map=children_map,
                )
                document = normalize_document(document)

                out.write(_dump_extended_json(document))
                out.write("\n")

                record_count += 1
                element.clear()

                if limit is not None and record_count >= limit:
                    break

    return record_count


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Convert MeSH DescriptorRecordSet XML to JSONL "
            "(one document per descriptor record, MongoDB extended JSON)."
        )
    )
    parser.add_argument(
        "input",
        nargs="?",
        default="desc2026.xml",
        type=Path,
        help="Input XML file (default: desc2026.xml)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output JSONL file (default: <input>.jsonl)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Convert only the first N descriptor records (useful for testing)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    input_path: Path = args.input
    output_path: Path = args.output or input_path.with_suffix(".jsonl")

    if not input_path.is_file():
        print(f"Input file not found: {input_path}", file=sys.stderr)
        return 1

    record_count = convert_xml_to_jsonl(
        input_path,
        output_path,
        limit=args.limit,
    )

    print(
        f"Wrote {record_count} document(s) to {output_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
