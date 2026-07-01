#!/usr/bin/env python3
"""Run a MeSH ancestor graphLookup and visualize the resolved graph structure."""

from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    import graphviz
except ImportError:  # pragma: no cover - handled in main()
    graphviz = None  # type: ignore[assignment]

try:
    from pymongo import MongoClient
    from pymongo.collection import Collection
except ImportError:  # pragma: no cover - handled in main()
    MongoClient = None  # type: ignore[misc, assignment]
    Collection = None  # type: ignore[misc, assignment]


DEFAULT_DATABASE = "romine"
DEFAULT_COLLECTION = "desc2026"
MAX_GRAPH_DEPTH = 15


def build_ancestor_pipeline(descriptor_name: str) -> list[dict[str, Any]]:
    return [
        {"$match": {"DescriptorName": descriptor_name}},
        {
            "$graphLookup": {
                "from": DEFAULT_COLLECTION,
                "startWith": "$parentDescriptorUIs",
                "connectFromField": "parentDescriptorUIs",
                "connectToField": "DescriptorUI",
                "as": "ancestors",
                "depthField": "level",
                "maxDepth": MAX_GRAPH_DEPTH,
            }
        },
        {
            "$project": {
                "DescriptorUI": 1,
                "DescriptorName": 1,
                "parentDescriptorUIs": 1,
                "treeNumbers": 1,
                "level": 1,
                "ancestors": {
                    "DescriptorUI": 1,
                    "DescriptorName": 1,
                    "level": 1,
                },
            }
        },
    ]


def get_connection_uri(explicit_uri: str | None) -> str:
    uri = explicit_uri or os.environ.get("MDB_MCP_CONNECTION_STRING")
    if not uri:
        raise SystemExit(
            "MongoDB connection URI required. Set MDB_MCP_CONNECTION_STRING or pass --uri."
        )
    return uri


def run_ancestor_query(
    collection: Collection,
    descriptor_name: str,
) -> dict[str, Any]:
    pipeline = build_ancestor_pipeline(descriptor_name)
    results = list(collection.aggregate(pipeline))
    if not results:
        raise SystemExit(f'No descriptor found with DescriptorName="{descriptor_name}".')
    if len(results) > 1:
        print(
            f'Warning: multiple descriptors matched "{descriptor_name}"; using the first.',
            file=sys.stderr,
        )
    return results[0]


def load_subgraph_nodes(
    collection: Collection,
    root_doc: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    ancestor_uis = [node["DescriptorUI"] for node in root_doc.get("ancestors", [])]
    all_uis = [root_doc["DescriptorUI"], *ancestor_uis]
    nodes = collection.find(
        {"DescriptorUI": {"$in": all_uis}},
        {
            "_id": 0,
            "DescriptorUI": 1,
            "DescriptorName": 1,
            "parentDescriptorUIs": 1,
            "treeNumbers": 1,
        },
    )
    return {node["DescriptorUI"]: node for node in nodes}


def collect_graph_edges(
    nodes: dict[str, dict[str, Any]],
) -> list[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    for ui, node in nodes.items():
        for parent_ui in node.get("parentDescriptorUIs", []):
            if parent_ui in nodes:
                edges.add((parent_ui, ui))
    return sorted(edges)


def find_root_nodes(nodes: dict[str, dict[str, Any]]) -> set[str]:
    return {
        ui
        for ui, node in nodes.items()
        if not any(parent in nodes for parent in node.get("parentDescriptorUIs", []))
    }


def build_root_paths(
    start_ui: str,
    nodes: dict[str, dict[str, Any]],
) -> list[list[tuple[str, str]]]:
    """Return all ancestor paths from the start node up to roots within the subgraph."""
    paths: list[list[tuple[str, str]]] = []

    def walk(ui: str, path: list[tuple[str, str]], visited: set[str]) -> None:
        node = nodes[ui]
        name = node["DescriptorName"]
        current_path = [*path, (ui, name)]
        parents = [parent for parent in node.get("parentDescriptorUIs", []) if parent in nodes]

        if not parents:
            paths.append(current_path)
            return

        for parent_ui in parents:
            if parent_ui in visited:
                continue
            walk(parent_ui, current_path, visited | {parent_ui})

    walk(start_ui, [], set())
    return paths


def compute_depth_from_roots(
    nodes: dict[str, dict[str, Any]],
    roots: set[str],
) -> dict[str, int]:
    depths = {root: 0 for root in roots}
    changed = True
    while changed:
        changed = False
        for ui, node in nodes.items():
            if ui in depths:
                continue
            parent_depths = [
                depths[parent]
                for parent in node.get("parentDescriptorUIs", [])
                if parent in depths
            ]
            if parent_depths:
                depths[ui] = max(parent_depths) + 1
                changed = True
    return depths


class _TrieNode:
    __slots__ = ("name", "children")

    def __init__(self, name: str) -> None:
        self.name = name
        self.children: dict[str, _TrieNode] = {}


def paths_to_trie(paths: list[list[tuple[str, str]]]) -> _TrieNode:
    root = _TrieNode(name="ROOT")
    for path in paths:
        node = root
        for ui, name in reversed(path):
            if ui not in node.children:
                node.children[ui] = _TrieNode(name=name)
            node = node.children[ui]
    return root


def render_ascii_tree(
    trie: _TrieNode,
    *,
    title: str,
) -> str:
    lines = [title, ""]

    def render(node: _TrieNode, prefix: str = "", is_last: bool = True) -> None:
        if node.name != "ROOT":
            branch = "└── " if is_last else "├── "
            lines.append(f"{prefix}{branch}{node.name}")
            prefix = prefix + ("    " if is_last else "│   ")

        child_items = list(node.children.items())
        for index, (_ui, child) in enumerate(child_items):
            render(child, prefix, index == len(child_items) - 1)

    render(trie)
    return "\n".join(lines)


def build_graphviz_digraph(
    *,
    nodes: dict[str, dict[str, Any]],
    edges: list[tuple[str, str]],
    target_ui: str,
    title: str,
    levels: dict[str, int] | None = None,
) -> Any:
    if graphviz is None:
        raise RuntimeError("graphviz package is required")

    roots = find_root_nodes(nodes)
    depths = compute_depth_from_roots(nodes, roots)

    dot = graphviz.Digraph(
        name="mesh_ancestor_graph",
        comment=title,
        graph_attr={
            "rankdir": "BT",
            "bgcolor": "white",
            "splines": "spline",
            "overlap": "false",
            "nodesep": "0.35",
            "ranksep": "0.55",
            "fontsize": "12",
            "label": title,
            "labelloc": "t",
        },
        node_attr={
            "shape": "box",
            "style": "rounded,filled",
            "fontname": "Helvetica",
            "fontsize": "11",
        },
        edge_attr={"arrowsize": "0.7", "color": "#4a5568"},
    )

    level_lookup = levels or {}
    for ui, node in nodes.items():
        name = node["DescriptorName"]
        label = f"{name}\\n{ui}"
        attrs: dict[str, str] = {
            "label": label,
            "fillcolor": "#e8f1fb",
        }

        if ui == target_ui:
            attrs["fillcolor"] = "#f6d365"
            attrs["penwidth"] = "2"
        elif ui in roots:
            attrs["fillcolor"] = "#c6f6d5"

        depth = depths.get(ui)
        if depth is not None:
            attrs["tooltip"] = f"depth={depth}"

        dot.node(ui, **attrs)

    for parent_ui, child_ui in edges:
        dot.edge(parent_ui, child_ui)

    # Keep nodes at the same depth aligned when possible.
    by_depth: dict[int, list[str]] = defaultdict(list)
    for ui, depth in depths.items():
        by_depth[depth].append(ui)
    for depth in sorted(by_depth):
        if len(by_depth[depth]) > 1:
            with dot.subgraph(name=f"rank_depth_{depth}") as same_rank:
                same_rank.attr(rank="same")
                for ui in by_depth[depth]:
                    same_rank.node(ui)

    return dot


def write_graph_outputs(
    dot: Any,
    output_stem: Path,
    *,
    formats: set[str],
) -> dict[str, Path]:
    output_stem.parent.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    dot_path = output_stem.with_suffix(".dot")
    dot.save(str(dot_path))
    written["dot"] = dot_path

    for fmt in sorted(formats):
        if fmt == "dot":
            continue
        rendered = output_stem.with_suffix(f".{fmt}")
        dot.render(
            filename=str(output_stem),
            format=fmt,
            cleanup=True,
        )
        written[fmt] = rendered

    return written


def render_html(
    trie: _TrieNode,
    *,
    title: str,
    descriptor: dict[str, Any],
    paths: list[list[tuple[str, str]]],
    graph_files: dict[str, Path] | None = None,
) -> str:
    level_counts: dict[int, int] = defaultdict(int)
    for ancestor in descriptor.get("ancestors", []):
        level_counts[ancestor.get("level", 0)] += 1

    def trie_to_list(node: _TrieNode) -> str:
        if not node.children:
            return ""
        items = []
        for ui, child in node.children.items():
            label = f"{child.name} <span class='ui'>({ui})</span>"
            nested = trie_to_list(child)
            if nested:
                items.append(f"<li>{label}<ul>{nested}</ul></li>")
            else:
                items.append(f"<li>{label}</li>")
        return "".join(items)

    path_rows = []
    for index, path in enumerate(paths, start=1):
        labels = " → ".join(name for _, name in reversed(path))
        path_rows.append(f"<tr><td>{index}</td><td>{labels}</td></tr>")

    tree_numbers = ", ".join(descriptor.get("treeNumbers", []))
    parents = ", ".join(descriptor.get("parentDescriptorUIs", []))

    graph_section = ""
    if graph_files:
        svg_path = graph_files.get("svg")
        png_path = graph_files.get("png")
        dot_path = graph_files.get("dot")
        if svg_path and svg_path.is_file():
            graph_section = f"""
  <h2>Graph (Graphviz)</h2>
  <div class="graph">{svg_path.read_text(encoding="utf-8")}</div>
"""
        elif png_path and png_path.is_file():
            graph_section = f"""
  <h2>Graph (Graphviz)</h2>
  <div class="graph"><img src="{png_path.name}" alt="MeSH ancestor graph" /></div>
"""
        if dot_path:
            graph_section += f"""
  <p class="dot-link"><a href="{dot_path.name}">Download DOT source</a></p>
"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>{title}</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 2rem;
      color: #1f2933;
      line-height: 1.5;
    }}
    h1, h2 {{ margin-bottom: 0.5rem; }}
    .meta, .summary {{ background: #f5f7fa; padding: 1rem; border-radius: 8px; }}
    .ui {{ color: #52606d; font-size: 0.9em; }}
    ul {{ list-style: none; padding-left: 1.25rem; }}
    ul li::before {{
      content: "▸ ";
      color: #3e7cb1;
    }}
    table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
    th, td {{ border: 1px solid #d9e2ec; padding: 0.5rem 0.75rem; text-align: left; }}
    th {{ background: #f0f4f8; }}
    .graph {{
      overflow-x: auto;
      border: 1px solid #d9e2ec;
      border-radius: 8px;
      padding: 1rem;
      background: white;
    }}
    .graph svg {{ max-width: 100%; height: auto; }}
    .dot-link {{ margin-top: 0.75rem; }}
  </style>
</head>
<body>
  <h1>{title}</h1>
  <div class="meta">
    <div><strong>DescriptorUI:</strong> {descriptor["DescriptorUI"]}</div>
    <div><strong>Tree numbers:</strong> {tree_numbers or "n/a"}</div>
    <div><strong>Direct parents:</strong> {parents or "none"}</div>
  </div>
  <h2>Ancestor summary</h2>
  <div class="summary">
    <div><strong>Ancestors returned by $graphLookup:</strong> {len(descriptor.get("ancestors", []))}</div>
    <div><strong>Distinct root-to-node paths:</strong> {len(paths)}</div>
    <div><strong>Levels:</strong> {", ".join(f"{level}: {count}" for level, count in sorted(level_counts.items()))}</div>
  </div>
  {graph_section}
  <h2>Tree (root → node)</h2>
  <ul>{trie_to_list(trie)}</ul>
  <h2>All resolved paths</h2>
  <table>
    <thead><tr><th>#</th><th>Path</th></tr></thead>
    <tbody>{"".join(path_rows)}</tbody>
  </table>
</body>
</html>
"""


def summarize_levels(ancestors: list[dict[str, Any]]) -> str:
    counts: dict[int, int] = defaultdict(int)
    for ancestor in ancestors:
        counts[ancestor.get("level", 0)] += 1
    return ", ".join(f"level {level}: {count}" for level, count in sorted(counts.items()))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Visualize MeSH ancestor graphs using MongoDB $graphLookup and Graphviz DOT."
    )
    parser.add_argument(
        "--descriptor",
        default="Calcimycin",
        help='DescriptorName to look up (default: "Calcimycin")',
    )
    parser.add_argument("--uri", help="MongoDB connection URI (or set MDB_MCP_CONNECTION_STRING)")
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--collection", default=DEFAULT_COLLECTION)
    parser.add_argument(
        "--output",
        type=Path,
        help="Output file stem without extension (default: mesh_graph_<DescriptorUI>)",
    )
    parser.add_argument(
        "--format",
        action="append",
        choices=("dot", "svg", "png", "html", "ascii"),
        help="Output format(s); repeatable (default: dot,svg,png,html)",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the generated HTML or SVG file in a browser",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the raw aggregation result as JSON",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if MongoClient is None:
        print("pymongo is required. Install it with: pip install pymongo", file=sys.stderr)
        return 1
    if graphviz is None:
        print("graphviz is required. Install it with: pip install graphviz", file=sys.stderr)
        return 1

    args = parse_args(argv)
    uri = get_connection_uri(args.uri)
    formats = set(args.format or ("dot", "svg", "png", "html"))

    client = MongoClient(uri)
    collection = client[args.database][args.collection]

    descriptor_doc = run_ancestor_query(collection, args.descriptor)
    nodes = load_subgraph_nodes(collection, descriptor_doc)
    edges = collect_graph_edges(nodes)
    paths = build_root_paths(descriptor_doc["DescriptorUI"], nodes)
    trie = paths_to_trie(paths)

    title = f'MeSH Ancestor Graph: {descriptor_doc["DescriptorName"]}'
    output_stem = args.output or Path(f'mesh_graph_{descriptor_doc["DescriptorUI"]}')

    levels = {
        ancestor["DescriptorUI"]: ancestor.get("level", 0)
        for ancestor in descriptor_doc.get("ancestors", [])
    }

    graph_files: dict[str, Path] = {}
    if formats.intersection({"dot", "svg", "png", "html"}):
        dot = build_graphviz_digraph(
            nodes=nodes,
            edges=edges,
            target_ui=descriptor_doc["DescriptorUI"],
            title=title,
            levels=levels,
        )
        graph_files = write_graph_outputs(
            dot,
            output_stem,
            formats=formats.intersection({"dot", "svg", "png"}),
        )

    if "ascii" in formats or not formats.isdisjoint({"html"}):
        ascii_tree = render_ascii_tree(trie, title=title)
        if "ascii" in formats:
            print(ascii_tree)
            print()
        print(f'Ancestors from $graphLookup: {len(descriptor_doc.get("ancestors", []))}')
        print(f'By level: {summarize_levels(descriptor_doc.get("ancestors", []))}')
        print(f'Graph nodes: {len(nodes)}, edges: {len(edges)}, paths to roots: {len(paths)}')

    if args.json:
        print()
        print(json.dumps(descriptor_doc, indent=2, default=str))

    if "html" in formats:
        html_path = output_stem.with_suffix(".html")
        html_path.write_text(
            render_html(
                trie,
                title=title,
                descriptor=descriptor_doc,
                paths=paths,
                graph_files=graph_files,
            ),
            encoding="utf-8",
        )
        graph_files["html"] = html_path
        print(f"\nHTML visualization written to {html_path.resolve()}")

    for fmt, path in sorted(graph_files.items()):
        if fmt != "html":
            print(f"{fmt.upper()} written to {path.resolve()}")

    if args.open:
        open_target = graph_files.get("html") or graph_files.get("svg")
        if open_target:
            webbrowser.open(open_target.resolve().as_uri())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
