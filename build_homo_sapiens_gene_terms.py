#!/usr/bin/env python3
"""Extract distinct Symbol and Synonyms values from homo_sapiens_gene_info.

Writes ``homo_sapiens_gene_terms.py`` for import by ``query_homo_sapiens.py``.
Re-run this script when the collection changes.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from pymongo import MongoClient

OUTPUT_PATH = Path(__file__).resolve().parent / "homo_sapiens_gene_terms.py"


def load_connection_uri() -> str:
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            if key.strip() == "MDB_MCP_CONNECTION_STRING":
                return value.strip()

    uri = os.environ.get("MDB_MCP_CONNECTION_STRING")
    if not uri:
        raise SystemExit(
            "MongoDB connection URI required. Set MDB_MCP_CONNECTION_STRING in .env or environment."
        )
    return uri


def collect_gene_terms() -> list[str]:
    db = MongoClient(load_connection_uri()).get_default_database()
    terms: set[str] = set()

    for doc in db.homo_sapiens_gene_info.find({}, {"Symbol": 1, "Synonyms": 1}):
        symbol = doc.get("Symbol")
        if symbol:
            terms.add(str(symbol))

        synonyms = doc.get("Synonyms")
        if synonyms:
            for part in str(synonyms).split("|"):
                value = part.strip()
                if value:
                    terms.add(value)

    return sorted(terms)


def write_terms_module(terms: list[str], path: Path = OUTPUT_PATH) -> None:
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        '"""Distinct homo_sapiens_gene_info Symbol and Synonyms values.',
        "",
        f"Auto-generated on {generated_at} by build_homo_sapiens_gene_terms.py.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        f"GENERATED_AT = {generated_at!r}",
        "",
        "GENE_SEARCH_TERMS: tuple[str, ...] = (",
    ]
    for term in terms:
        lines.append(f"    {term!r},")
    lines.extend([")", ""])

    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    terms = collect_gene_terms()
    write_terms_module(terms)
    print(f"Wrote {len(terms):,} gene terms to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
