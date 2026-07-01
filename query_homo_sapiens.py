#!/usr/bin/env python3
"""Search content for a user phrase and homo_sapiens gene symbols, then export HTML.

Gene terms are loaded from the static module ``homo_sapiens_gene_terms`` (Symbol and
Synonyms from ``homo_sapiens_gene_info``). Regenerate that file with
``build_homo_sapiens_gene_terms.py`` when the collection changes.

The user phrase uses the Atlas Search ``phrase`` operator on ``body_text``. Gene symbols
use ``scientificGeneAnalyzer``. This script does not use ``gene_synonyms`` or Atlas
``synonyms`` at query time.
"""

from __future__ import annotations

import html
import os
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from pymongo import MongoClient

from homo_sapiens_gene_terms import GENERATED_AT, GENE_SEARCH_TERMS
from scientific_analyzer import (
    BODY_TEXT_MULTI_NAME,
    body_text_path,
    body_text_phrase_clause,
    body_text_text_clause,
    tokenize,
)

# USER_TERM = "glioblastoma"
USER_TERM = "Pancreatic Cancer"
RESULT_LIMIT = 10
HIGHLIGHT_PASSAGES = 5
OUTPUT_HTML = "search_results_homo_sapiens.html"
SEARCH_INDEX = "default"

T = TypeVar("T")


def measure_query_time(label: str, operation: Callable[[], T]) -> tuple[T, float]:
    """Run a MongoDB operation and return its result with elapsed wall time in seconds."""
    start = time.perf_counter()
    result = operation()
    elapsed = time.perf_counter() - start
    print(f"{label}: {elapsed:.3f}s")
    return result, elapsed


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


def build_search_pipeline(search_terms: list[str]) -> list[dict[str, Any]]:
    """Build the content search pipeline."""
    body_path = body_text_path(BODY_TEXT_MULTI_NAME)
    return [
        {
            "$search": {
                "index": SEARCH_INDEX,
                "compound": {
                    "must": [
                        body_text_phrase_clause(USER_TERM),
                        body_text_text_clause(search_terms),
                    ],
                },
                "highlight": {
                    "path": ["body_text", body_path],
                    "maxNumPassages": HIGHLIGHT_PASSAGES,
                },
            }
        },
        {"$limit": RESULT_LIMIT},
        {
            "$project": {
                "title": 1,
                "body_text": 1,
                "score": {"$meta": "searchScore"},
                "highlights": {"$meta": "searchHighlights"},
            }
        },
    ]


def render_highlight_snippet(texts: list[dict[str, str]], user_term: str) -> str:
    """Convert Atlas Search highlight texts into safe HTML."""
    user_term_lower = user_term.casefold()
    user_words = [word for word in user_term.split() if word]
    user_word_set = {word.casefold() for word in user_words}
    parts: list[str] = []

    for fragment in texts:
        value = html.escape(fragment.get("value", ""))
        if fragment.get("type") == "hit":
            value_fold = fragment.get("value", "").casefold()
            if len(user_words) <= 1:
                is_user_term = value_fold == user_term_lower
            else:
                is_user_term = value_fold in user_word_set
            css_class = "hit-term" if is_user_term else "hit-gene"
            parts.append(f'<mark class="{css_class}">{value}</mark>')
        else:
            parts.append(value)

    return "".join(parts)


def find_user_term_forms(body_text: str, user_term: str) -> list[str]:
    """Surface forms of the user phrase in body_text (case-insensitive, slop 0)."""
    if not body_text or not user_term:
        return []
    words = user_term.split()
    if not words:
        return []
    pattern = re.compile(
        r"\b" + r"\s+".join(re.escape(word) for word in words) + r"\b",
        re.IGNORECASE,
    )
    return sorted({match.group(0) for match in pattern.finditer(body_text)})


def find_gene_matches(body_text: str, gene_terms: set[str]) -> list[str]:
    """Unique gene tokens in body_text (case-sensitive scientific tokenization)."""
    if not body_text or not gene_terms:
        return []
    return sorted({token for token in tokenize(body_text) if token in gene_terms})


def render_match_chips(terms: list[str], *, css_class: str) -> str:
    if not terms:
        return '<p class="empty">None</p>'
    return "".join(
        f'<span class="match-chip {css_class}">{html.escape(term)}</span>'
        for term in terms
    )


def render_all_matches_section(
    body_text: str,
    *,
    user_term: str,
    gene_terms: set[str],
) -> str:
    user_matches = find_user_term_forms(body_text, user_term)
    gene_matches = find_gene_matches(body_text, gene_terms)
    total = len(user_matches) + len(gene_matches)
    return f"""
              <section class="all-matches">
                <h3>All matches ({total:,})</h3>
                <div class="match-group">
                  <h4>User phrase ({len(user_matches):,})</h4>
                  <div class="match-chips">
                    {render_match_chips(user_matches, css_class="match-chip-user")}
                  </div>
                </div>
                <div class="match-group">
                  <h4>Genes ({len(gene_matches):,})</h4>
                  <div class="match-chips">
                    {render_match_chips(gene_matches, css_class="match-chip-gene")}
                  </div>
                </div>
              </section>
    """


def render_results_html(
    results: list[dict[str, Any]],
    *,
    user_term: str,
    gene_terms: set[str],
    search_term_count: int,
    terms_generated_at: str,
    search_elapsed: float,
) -> str:
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    result_cards: list[str] = []

    for index, doc in enumerate(results, start=1):
        title = html.escape(str(doc.get("title", "Untitled")))
        score = doc.get("score")
        score_text = f"{score:.4f}" if isinstance(score, (int, float)) else "n/a"
        passages: list[str] = []

        for passage in doc.get("highlights", []):
            texts = passage.get("texts", [])
            if not texts:
                continue
            snippet = render_highlight_snippet(texts, user_term)
            passage_score = passage.get("score")
            score_label = (
                f'<span class="passage-score">passage score: {passage_score:.4f}</span>'
                if isinstance(passage_score, (int, float))
                else ""
            )
            passages.append(
                f'<blockquote class="passage">{snippet}{score_label}</blockquote>'
            )

        if not passages:
            passages.append('<p class="empty">No highlight passages returned.</p>')

        body_text = str(doc.get("body_text") or "")
        all_matches = render_all_matches_section(
            body_text,
            user_term=user_term,
            gene_terms=gene_terms,
        )

        result_cards.append(
            f"""
            <article class="result">
              <header>
                <h2>{index}. {title}</h2>
                <div class="result-meta">
                  <span><strong>Score:</strong> {score_text}</span>
                  <span><strong>Document ID:</strong> {html.escape(str(doc.get("_id", "")))}</span>
                </div>
              </header>
              <section class="highlights">
                <h3>Matched excerpts</h3>
                {"".join(passages)}
              </section>
              {all_matches}
            </article>
            """
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Atlas Search Results: {html.escape(user_term)}</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 2rem auto;
      max-width: 960px;
      color: #1f2933;
      line-height: 1.6;
      background: #f8fafc;
    }}
    h1, h2, h3 {{ margin-bottom: 0.5rem; }}
    .meta, .legend {{
      background: #eef2f7;
      padding: 1rem;
      border-radius: 8px;
      margin-bottom: 1.5rem;
    }}
    .meta div, .legend div {{ margin: 0.25rem 0; }}
    .result {{
      background: white;
      border: 1px solid #d9e2ec;
      border-radius: 10px;
      padding: 1.25rem;
      margin-bottom: 1.5rem;
      box-shadow: 0 1px 2px rgba(16, 24, 40, 0.05);
    }}
    .result-meta {{
      color: #52606d;
      font-size: 0.95rem;
      display: flex;
      gap: 1.5rem;
      flex-wrap: wrap;
    }}
    .passage {{
      margin: 0.75rem 0;
      padding: 0.9rem 1rem;
      border-left: 4px solid #3e7cb1;
      background: #f8fbff;
      border-radius: 0 8px 8px 0;
    }}
    .passage-score {{
      display: block;
      margin-top: 0.75rem;
      color: #7b8794;
      font-size: 0.85rem;
    }}
    mark {{
      padding: 0.05rem 0.15rem;
      border-radius: 3px;
    }}
    .hit-term {{
      background: #ffe066;
      color: #102a43;
    }}
    .hit-gene {{
      background: #7dd3fc;
      color: #102a43;
    }}
    .legend span {{
      display: inline-block;
      margin-right: 1rem;
    }}
    .empty {{
      color: #7b8794;
      font-style: italic;
    }}
    .all-matches {{
      margin-top: 1.25rem;
      padding-top: 1rem;
      border-top: 1px solid #d9e2ec;
    }}
    .match-group {{
      margin: 0.75rem 0 1rem;
    }}
    .match-group h4 {{
      margin: 0 0 0.5rem;
      font-size: 0.95rem;
      color: #52606d;
    }}
    .match-chips {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.35rem;
    }}
    .match-chip {{
      display: inline-block;
      padding: 0.15rem 0.45rem;
      border-radius: 999px;
      font-size: 0.85rem;
      line-height: 1.4;
      border: 1px solid transparent;
    }}
    .match-chip-user {{
      background: #fff3bf;
      border-color: #f0d675;
      color: #102a43;
    }}
    .match-chip-gene {{
      background: #e0f2fe;
      border-color: #7dd3fc;
      color: #102a43;
    }}
  </style>
</head>
<body>
  <h1>Atlas Search Results</h1>
  <div class="meta">
    <div><strong>User term:</strong> {html.escape(user_term)}</div>
    <div><strong>Gene search terms:</strong> {search_term_count:,}</div>
    <div><strong>Results:</strong> {len(results)}</div>
    <div><strong>body_text analyzer:</strong> {html.escape(BODY_TEXT_MULTI_NAME)} (case-sensitive scientific_gene_analyzer)</div>
    <div><strong>User phrase:</strong> <code>phrase</code> operator on body_text (lucene.standard, slop 0)</div>
    <div><strong>Gene terms source:</strong> homo_sapiens_gene_info Symbol + Synonyms (static import)</div>
    <div><strong>Gene terms generated:</strong> {html.escape(terms_generated_at)}</div>
    <div><strong>Generated:</strong> {generated_at}</div>
    <div><strong>Content $search query:</strong> {search_elapsed:.3f}s</div>
  </div>
  <div class="legend">
    <div><span><mark class="hit-term">user term</mark></span>
         <span><mark class="hit-gene">gene hit</mark></span></div>
  </div>
  {"".join(result_cards)}
</body>
</html>
"""


def main() -> None:
    search_terms = list(GENE_SEARCH_TERMS)
    gene_terms = set(search_terms)
    print(f"Loaded {len(search_terms):,} gene terms from homo_sapiens_gene_terms.py")

    client = MongoClient(load_connection_uri())
    db = client.get_default_database()

    pipeline = build_search_pipeline(search_terms)
    results, search_elapsed = measure_query_time(
        "Content $search query",
        lambda: list(db.content.aggregate(pipeline)),
    )

    output_path = Path(__file__).resolve().parent / OUTPUT_HTML
    html_content = render_results_html(
        results,
        user_term=USER_TERM,
        gene_terms=gene_terms,
        search_term_count=len(search_terms),
        terms_generated_at=GENERATED_AT,
        search_elapsed=search_elapsed,
    )
    output_path.write_text(html_content, encoding="utf-8")

    print(f"Wrote {len(results)} highlighted results to {output_path}")


if __name__ == "__main__":
    main()
