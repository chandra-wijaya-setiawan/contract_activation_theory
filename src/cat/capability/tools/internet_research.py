"""internet_research — low-level tool: search → browse → extract.

Pure function, no LLM. Uses DuckDuckGo HTML search + trafilatura extraction.
Fast (~2s per search + ~0.5s per page), no API keys, no browser needed.

The agent (internet_researcher profile) decides WHAT to research and HOW
to synthesize. This tool just does the mechanical work.
"""
from __future__ import annotations

import html as html_mod
import json
import re
import time
from datetime import date
from typing import Any

import httpx

from cat.capability.registry import Capability, ToolResult
from cat.contract.model import State

_OUTPUT_CAP = 12_000
_DDG_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0 Safari/537.36"
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Search query (DuckDuckGo)",
        },
        "urls": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Optional: specific URLs to browse instead of searching. "
                "If provided, skip search and go straight to browsing."
            ),
        },
        "n_results": {
            "type": "integer",
            "description": "Number of search results to browse (default 3, max 8)",
        },
        "extract": {
            "type": "string",
            "enum": ["text", "links", "both"],
            "description": "What to extract from each page (default: text)",
        },
    },
    "required": ["query"],
}


def create_internet_research_capability() -> Capability:
    return Capability(
        name="internet_research",
        description=(
            "Low-level web research: search → browse → extract. No LLM.\n\n"
            "Searches DuckDuckGo, visits result pages, and returns clean text.\n"
            "Fast (~2s search + ~0.5s/page), no API keys needed.\n\n"
            "Args:\n"
            "  query       — search query (required)\n"
            "  urls        — optional specific URLs to browse (skip search)\n"
            "  n_results   — number of results to browse (default 3, max 8)\n"
            "  extract     — 'text', 'links', or 'both' (default: text)\n\n"
            "Returns: raw page content with URLs and titles for synthesis."
        ),
        json_schema=_SCHEMA,
        handler=_handle,
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _truncate(s: str, cap: int) -> str:
    enc = s.encode("utf-8", errors="replace")
    if len(enc) <= cap:
        return s
    return enc[:cap].decode("utf-8", errors="replace") + f"\n... [truncated at {cap} bytes]"


def _today() -> str:
    return date.today().isoformat()


def _strip_html(raw: str) -> str:
    text = re.sub(
        r'<(script|style|nav|header|footer)[^>]*>.*?</\1>',
        '', raw, flags=re.S | re.I,
    )
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return html_mod.unescape(text).strip()


# ── Search: DuckDuckGo HTML ─────────────────────────────────────────────────

def _search_ddg(query: str, n: int) -> list[dict]:
    """Search DuckDuckGo HTML endpoint. Returns [{title, url, snippet}]."""
    try:
        r = httpx.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={"User-Agent": _DDG_UA},
            timeout=15,
            follow_redirects=True,
        )
        if r.status_code != 200:
            return []
    except Exception:
        return []

    html = r.text
    results: list[dict] = []

    # Parse result blocks: each has a title link + snippet
    # Pattern: <a rel="nofollow" class="result__a" href="URL">TITLE</a>
    #          followed by <a class="result__snippet">SNIPPET</a>
    blocks = re.findall(
        r'class="result__title".*?'
        r'href="([^"]+)".*?'
        r'>(.*?)</a>.*?'
        r'class="result__snippet"[^>]*>(.*?)(?:</a>|</div>)',
        html, re.DOTALL,
    )

    for url, title, snippet in blocks[:n]:
        # Clean up
        title = re.sub(r'<[^>]+>', '', title).strip()
        snippet = re.sub(r'<[^>]+>', '', snippet).strip()
        url = html_mod.unescape(url)
        # DDG sometimes wraps URLs in redirects
        if url.startswith("//duckduckgo.com/l/?"):
            m = re.search(r'uddg=([^&]+)', url)
            if m:
                url = html_mod.unescape(m.group(1))
        if title and url:
            results.append({"title": title, "url": url, "snippet": snippet})

    return results


# ── Browse: fetch + extract ─────────────────────────────────────────────────

def _browse_page(url: str) -> tuple[str, str]:
    """Fetch a page and extract clean text. Returns (content, error)."""
    try:
        r = httpx.get(
            url,
            headers={"User-Agent": _DDG_UA},
            timeout=20,
            follow_redirects=True,
        )
        if r.status_code != 200:
            return "", f"HTTP {r.status_code}"
    except httpx.TimeoutException:
        return "", "timeout"
    except Exception as exc:
        return "", str(exc)

    raw = r.text

    # Try trafilatura first (best article extraction)
    try:
        from trafilatura import extract
        text = extract(
            raw,
            include_comments=False,
            include_tables=True,
            deduplicate=True,
            favor_recall=False,
        )
        if text and len(text.strip()) > 100:
            return text.strip(), ""
    except Exception:
        pass

    # Fallback: strip HTML manually
    return _strip_html(raw), ""


# ── Handler ──────────────────────────────────────────────────────────────────

def _handle(args: dict[str, Any], state: State) -> ToolResult:
    query = (args.get("query") or "").strip()
    if not query:
        return ToolResult(tool="internet_research", is_error=True, output="missing 'query'")

    urls = args.get("urls") or []
    n_results = min(int(args.get("n_results") or 3), 8)
    extract = args.get("extract", "text")

    pages: list[dict] = []

    # Step 1: Get URLs
    if urls:
        search_results = [
            {"title": u.split("/")[-1].replace("_", " ").replace("-", " "), "url": u, "snippet": ""}
            for u in urls
        ]
    else:
        search_results = _search_ddg(query, n_results)

    if not search_results:
        return ToolResult(
            tool="internet_research",
            is_error=True,
            output=f"No search results for: {query}",
            detail={"query": query},
        )

    # Step 2: Browse each result page
    for r in search_results[:n_results]:
        url = r["url"]
        content, err = _browse_page(url)
        pages.append({
            "title": r.get("title", url),
            "url": url,
            "snippet": r.get("snippet", ""),
            "content": content if not err else f"(fetch failed: {err})",
        })

    # Step 3: Format output
    lines = [
        f'# Raw Research Results for: "{query}"',
        f'Retrieved: {_today()}',
        f'Sources: {len(pages)}',
        "",
    ]

    for i, page in enumerate(pages, 1):
        lines.append(f"## [{i}] {page['title']}")
        lines.append(f"URL: {page['url']}")
        if page["snippet"]:
            lines.append(f"Snippet: {page['snippet']}")
        lines.append("")
        content = page["content"]
        if content and not content.startswith("(fetch failed"):
            lines.append(content[:2000])
        else:
            lines.append(content)
        lines.append("")
        lines.append("---")
        lines.append("")

    return ToolResult(
        tool="internet_research",
        is_error=False,
        output=_truncate("\n".join(lines), _OUTPUT_CAP),
        detail={
            "query": query,
            "n_results": len(pages),
            "sources": [{"title": p["title"], "url": p["url"]} for p in pages],
        },
    )
