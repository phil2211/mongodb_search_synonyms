#!/usr/bin/env python3
"""Scientific-text tokenizer pattern for Atlas Search gene-aware indexing.

Design goals
------------
1. Keep HGNC gene symbols as a single token (BRCA1, A1BG-AS1, HOXA@, …).
2. Tokenize all other scientific prose into searchable words (cancer, gefitinib, …).
3. Avoid the standard analyzer splitting letter–number boundaries (BRCA1 → brca + 1).

The MongoDB index uses ``regexCaptureGroup`` with the pattern below. Each alternation
branch is tried left-to-right; the first match wins, then scanning continues.

Synonym documents use the built-in ``lucene.keyword`` analyzer (case-sensitive) so each
``gene_synonyms`` entry is exactly one token. Body text uses ``scientific_gene_analyzer``
via the ``body_text`` multi field without lowercasing, so gene/synonym OR queries match
case-sensitively (``FOR`` does not match prose ``for``).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# --- Pattern parts (Java regex / Atlas Search) --------------------------------

# Mitochondrial-style symbols with an internal space (12S rRNA, 16S rRNA).
GENE_WITH_SPACE = r"\d+\p{L}+\s+[\p{L}\p{N}]+"

# Prime-labelled regions common in RNA papers (5'-UTR, 3'-UTR).
PRIME_REGION = r"\d+'[-][\p{L}\p{N}]+"

# Quoted cell lines / constructs in papers ('SW1990', 'Bxpc-3').
QUOTED_TERM = r"'[\p{L}\p{M}\p{N}@_.+-]+'"

# Genes and general words: alphanumeric runs; internal - . ' · @ : + _ / ( ) connectors.
# Includes / and () so synonym names like C/EBP-alpha and DAGL(ALPHA) stay intact.
# Letter–number sequences (BRCA1, TP53) stay intact because \p{L}\p{N} share one group.
WORD_OR_GENE = r"[\p{L}\p{M}\p{N}@_()/]+(?:[-'.·:@+_/()][\p{L}\p{M}\p{N}@_()/]+)*"

TOKEN_PATTERN = (
    f"({GENE_WITH_SPACE}|{PRIME_REGION}|{QUOTED_TERM}|{WORD_OR_GENE})"
)

# Python equivalent for offline validation (Java \p{} → Unicode alnum + @ _).
_PY_TOKEN_PATTERN = (
    r"(\d+[^\W_]+\s+[^\W_]+"
    r"|\d+'[-][^\W_]+"
    r"|'[\w@.+-]+'"
    r"|[\w@()/]+(?:[-'.·:@+_/()][\w@()/]+)*)"
)
_PYTHON_TOKENIZER = re.compile(_PY_TOKEN_PATTERN, re.UNICODE)

MIN_TOKEN_LENGTH = 2
BODY_TEXT_MULTI_NAME = "scientificGeneAnalyzer"
SYNONYM_MAPPING_NAME = "synonym_mapping"
SYNONYM_ANALYZER = "lucene.keyword"
INDEX_DEFINITION_PATH = Path(__file__).resolve().parent / "atlas_search_index_scientific.json"


def apply_token_filters(
    tokens: list[str],
    *,
    lowercase: bool = False,
) -> list[str]:
    """Mirror index token filters: optional lowercase, strip quotes, min length."""
    filtered: list[str] = []
    for token in tokens:
        value = token.strip("'")
        if lowercase:
            value = value.lower()
        if len(value) >= MIN_TOKEN_LENGTH:
            filtered.append(value)
    return filtered


def tokenize(text: str, *, lowercase: bool = False) -> list[str]:
    """Tokenize text using the Python approximation of the Atlas pattern."""
    return apply_token_filters(
        (m.group(1) for m in _PYTHON_TOKENIZER.finditer(text)),
        lowercase=lowercase,
    )


def searchable_char_indexes(text: str) -> set[int]:
    """Indexes of characters we expect a tokenizer to cover."""
    indexes: set[int] = set()
    for index, char in enumerate(text):
        if char.isalnum() or char in "-'.·:@+_/()":
            indexes.add(index)
        elif char == "'" and index + 1 < len(text):
            # Opening quote of a quoted term.
            indexes.add(index)
    return indexes


def coverage_gaps(text: str) -> list[int]:
    covered: set[int] = set()
    for match in _PYTHON_TOKENIZER.finditer(text):
        covered.update(range(match.start(), match.end()))
    return sorted(searchable_char_indexes(text) - covered)


def build_analyzer_definition() -> dict[str, Any]:
    return {
        "name": "scientific_gene_analyzer",
        "tokenizer": {
            "type": "regexCaptureGroup",
            "pattern": TOKEN_PATTERN,
            "group": 1,
        },
        "tokenFilters": [
            {
                "type": "regex",
                "pattern": "^'+|'+$",
                "replacement": "",
                "matches": "all",
            },
            {"type": "length", "min": MIN_TOKEN_LENGTH, "max": 256},
        ],
    }


def body_text_path(multi: str | None = None) -> str | dict[str, str]:
    """Atlas Search path for body_text; pass multi name to select an alternate analyzer."""
    if multi is None:
        return "body_text"
    return {"value": "body_text", "multi": multi}


def body_text_text_clause(
    query: str | list[str],
    *,
    synonyms: str | None = None,
    multi: str | None = BODY_TEXT_MULTI_NAME,
) -> dict[str, Any]:
    """Build a text operator clause for body_text."""
    clause: dict[str, Any] = {
        "path": body_text_path(multi),
        "query": query,
    }
    if synonyms is not None:
        clause["synonyms"] = synonyms
    return {"text": clause}


def body_text_phrase_clause(
    query: str,
    *,
    slop: int | None = None,
) -> dict[str, Any]:
    """Build a ``phrase`` operator clause for ``body_text``.

    See: https://www.mongodb.com/docs/atlas/atlas-search/phrase/
    """
    clause: dict[str, Any] = {
        "query": query,
        "path": "body_text",
    }
    if slop is not None:
        clause["slop"] = slop
    return {"phrase": clause}


def body_text_user_term_clause(query: str, *, slop: int | None = None) -> dict[str, Any]:
    """Alias for :func:`body_text_phrase_clause`."""
    return body_text_phrase_clause(query, slop=slop)


def build_index_definition() -> dict[str, Any]:
    return {
        "analyzers": [build_analyzer_definition()],
        "mappings": {
            "dynamic": False,
            "fields": {
                "abstract_text": {"type": "string"},
                "body_text": {
                    "type": "string",
                    "analyzer": "lucene.standard",
                    "searchAnalyzer": "lucene.standard",
                    "indexOptions": "offsets",
                    "multi": {
                        BODY_TEXT_MULTI_NAME: {
                            "type": "string",
                            "analyzer": "scientific_gene_analyzer",
                            "searchAnalyzer": "scientific_gene_analyzer",
                        }
                    },
                },
                "figure_captions": {"type": "string"},
                "title": {"type": "string"},
                "data_provider": {"type": "token"},
                "doc_type": {"type": "token"},
                "granted_rights": {
                    "type": "document",
                    "fields": {
                        "ai_finetuning": {"type": "boolean"},
                        "ai_inference": {"type": "boolean"},
                        "ai_training": {"type": "boolean"},
                        "perpetual_access": {"type": "boolean"},
                    },
                },
                "issn": {
                    "type": "document",
                    "fields": {"value": {"type": "token"}},
                },
                "issue": {"type": "token"},
                "last_record_update": {"type": "date"},
                "publication_date": {"type": "date"},
                "source_title": {"type": "token"},
                "volume": {"type": "token"},
            },
        },
        "synonyms": [
            {
                "name": "synonym_mapping",
                "analyzer": SYNONYM_ANALYZER,
                "source": {"collection": "gene_synonyms"},
            }
        ],
    }


def write_index_definition(path: Path = INDEX_DEFINITION_PATH) -> None:
    path.write_text(
        json.dumps(build_index_definition(), indent=2) + "\n",
        encoding="utf-8",
    )


def standard_like_split(text: str) -> list[str]:
    """Approximate lucene.standard splitting on letter/number boundaries."""
    return re.findall(r"[A-Za-z]+|\d+", text)


def synonym_tokenize(term: str) -> list[str]:
    """Approximate lucene.keyword (entire string as one token, case preserved)."""
    return [term] if term else []


def validate_synonym_terms(terms: list[str]) -> dict[str, list[tuple[str, list[str]]]]:
    """Check synonym strings tokenize to a single token for each analyzer."""
    keyword_failures: list[tuple[str, list[str]]] = []
    scientific_failures: list[tuple[str, list[str]]] = []
    for term in terms:
        keyword_tokens = synonym_tokenize(term)
        scientific_tokens = tokenize(term)
        if len(keyword_tokens) != 1:
            keyword_failures.append((term, keyword_tokens))
        if len(scientific_tokens) != 1:
            scientific_failures.append((term, scientific_tokens))
    return {
        "keyword_failures": keyword_failures,
        "scientific_failures": scientific_failures,
    }


def validate_symbols(symbols: list[str]) -> list[tuple[str, list[str]]]:
    failures: list[tuple[str, list[str]]] = []
    for symbol in symbols:
        tokens = tokenize(symbol)
        if tokens != [symbol]:
            failures.append((symbol, tokens))
    return failures


def print_demo() -> None:
    examples = [
        "Mutations in BRCA1 and TP53 drive cancer progression.",
        "The A1BG-AS1 lncRNA regulates A2M expression.",
        "IL-6 and TNF-α signaling via β-catenin/Wnt pathway.",
        "miR-21-5p sponges PTEN in COVID-19 patients.",
        "5'-UTR and 3'-UTR regions; HOXA@ and HHC2:065915.",
        "Cell lines 'SW1990' and 'Bxpc-3' were used.",
    ]
    print("=== Tokenization vs standard-like split ===\n")
    for example in examples:
        print(example)
        print(f"  scientific: {tokenize(example)}")
        print(f"  standard:   {standard_like_split(example)}")
        print()


def main() -> None:
    from pymongo import MongoClient

    write_index_definition()
    print(f"Wrote {INDEX_DEFINITION_PATH}\n")
    print_demo()

    env_path = Path(__file__).resolve().parent / ".env"
    uri = None
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("MDB_MCP_CONNECTION_STRING="):
                uri = line.strip().split("=", 1)[1]
                break
    if not uri:
        print("Set MDB_MCP_CONNECTION_STRING to run database validation.")
        return

    client = MongoClient(uri)
    db = client.get_default_database()
    symbols = sorted({
        doc["Symbol"]
        for doc in db.homo_sapiens_gene_info.find(
            {"Symbol": {"$exists": True, "$ne": ""}},
            {"Symbol": 1},
        )
        if doc.get("Symbol")
    })

    failures = validate_symbols(symbols)
    print(f"Gene symbols: {len(symbols)} unique")
    print(f"Single-token capture failures: {len(failures)}")
    if failures:
        print("  examples:", failures[:10])

    synonym_terms = sorted({
        term
        for doc in db.gene_synonyms.find({}, {"input": 1, "synonyms": 1})
        for field in ("input", "synonyms")
        for term in (
            doc[field] if isinstance(doc.get(field), list) else [doc.get(field)]
        )
        if term
    })
    synonym_report = validate_synonym_terms(synonym_terms)
    print(f"\nSynonym terms: {len(synonym_terms)}")
    print(
        f"Keyword analyzer failures: {len(synonym_report['keyword_failures'])} "
        f"({SYNONYM_ANALYZER})"
    )
    print(
        f"Scientific analyzer single-token failures: "
        f"{len(synonym_report['scientific_failures'])} "
        f"(terms with spaces; body text may use separate tokens)"
    )
    print(f"C/EBP-alpha scientific tokens: {tokenize('C/EBP-alpha')}")
    print(f"C/EBP-alpha keyword tokens: {synonym_tokenize('C/EBP-alpha')}")
    if synonym_report["scientific_failures"]:
        print("  scientific multi-token examples:", synonym_report["scientific_failures"][:5])

    total_missed = 0
    total_searchable = 0
    for doc in db.content.find({"body_text": {"$exists": True}}, {"body_text": 1}).limit(20):
        text = doc["body_text"][:10_000]
        searchable = searchable_char_indexes(text)
        missed = coverage_gaps(text)
        total_missed += len(missed)
        total_searchable += len(searchable)

    pct = 100 * (1 - total_missed / total_searchable) if total_searchable else 100
    print(
        f"Body-text coverage (20 samples, first 10k chars): "
        f"{pct:.2f}% of searchable characters captured "
        f"({total_missed} chars missed, mostly isolated punctuation in citations)"
    )


if __name__ == "__main__":
    main()
