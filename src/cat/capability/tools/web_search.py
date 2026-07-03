"""web_search capability — structured web search using DuckDuckGo HTML.

Primary source: DuckDuckGo HTML search (no API key, no browser, ~1s).
Fallback: Wikipedia search API.

Returns title + snippet + URL for each result.
For full page content, follow up with web_browse(url=...).
"""
from __future__ import annotations

import html as html_mod
import json
import re
import subprocess
import urllib.parse
from typing import Any

import httpx

from cat.capability.registry import Capability, ToolResult
from cat.contract.model import State

_OUTPUT_CAP = 6_000
_DDG_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0 Safari/537.36"
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Search query (name, topic, question, etc.)",
        },
        "n_results": {
            "type": "integer",
            "description": "Max results to return (default 5, max 10)",
        },
    },
    "required": ["query"],
}


def create_web_search_capability() -> Capability:
    return Capability(
        name="web_search",
        description=(
            "Search the web and return result titles, URLs, and snippets. "
            "Uses DuckDuckGo HTML (fast, no API key). "
            "Good for: recent news, releases, people, places, concepts, facts. "
            "Follow up with web_browse(url=...) to read a full page."
        ),
        json_schema=_SCHEMA,
        handler=_handle,
    )


def _truncate(s: str, cap: int) -> str:
    enc = s.encode("utf-8", errors="replace")
    if len(enc) <= cap:
        return s
    return enc[:cap].decode("utf-8", errors="replace") + f"\n... [truncated at {cap} bytes]"


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

    html_text = r.text
    results: list[dict] = []

    blocks = re.findall(
        r'class="result__title".*?'
        r'href="([^"]+)".*?'
        r'>(.*?)</a>.*?'
        r'class="result__snippet"[^>]*>(.*?)(?:</a>|</div>)',
        html_text, re.DOTALL,
    )

    for url, title, snippet in blocks[:n]:
        title = re.sub(r'<[^>]+>', '', title).strip()
        snippet = re.sub(r'<[^>]+>', '', snippet).strip()
        url = html_mod.unescape(url)
        if url.startswith("//duckduckgo.com/l/?"):
            m = re.search(r'uddg=([^&]+)', url)
            if m:
                url = html_mod.unescape(m.group(1))
        if title and url:
            results.append({"title": title, "url": url, "snippet": snippet})

    return results


def _search_wikipedia(query: str, n: int) -> list[dict]:
    """Search Wikipedia via its free API. Returns [{title, url, snippet}]."""
    params = urllib.parse.urlencode({
        "action": "query", "list": "search", "srsearch": query,
        "format": "json", "srlimit": n, "srprop": "snippet|titlesnippet",
    })
    url = f"https://en.wikipedia.org/w/api.php?{params}"
    cmd = [
        "curl", "-s", "--max-time", "15",
        "-H", "Accept: application/json",
        "-H", "User-Agent: CAT-Agent/1.0",
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if proc.returncode != 0:
            return []
        data = json.loads(proc.stdout)
        hits = data.get("query", {}).get("search", [])
        return [{
            "title": h.get("title", ""),
            "url": f"https://en.wikipedia.org/wiki/{h.get('title', '').replace(' ', '_')}",
            "snippet": re.sub(r"<[^>]+>", "", h.get("snippet", "")).strip(),
        } for h in hits]
    except Exception:
        return []


def _handle(args: dict[str, Any], state: State) -> ToolResult:
    query = (args.get("query") or "").strip()
    if not query:
        return ToolResult(tool="web_search", is_error=True, output="missing 'query'")

    n = min(int(args.get("n_results") or 5), 10)

    # Primary: DuckDuckGo HTML
    results = _search_ddg(query, n)
    source = "duckduckgo"

    # Fallback: Wikipedia
    if not results:
        results = _search_wikipedia(query, n)
        source = "wikipedia"

    if not results:
        return ToolResult(
            tool="web_search",
            is_error=True,
            output=f"No search results for: {query}",
            detail={"query": query},
        )

    lines = [f'=== Web Search: "{query}" ===', ""]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}")
        if r.get("url"):
            lines.append(f"   {r['url']}")
        if r.get("snippet"):
            lines.append(f"   {r['snippet']}")
        lines.append("")

    lines.append("Tip: use web_browse(url='<URL above>') to read the full page.")

    return ToolResult(
        tool="web_search",
        is_error=False,
        output=_truncate("\n".join(lines), _OUTPUT_CAP),
        detail={"query": query, "n_results": len(results), "source": source},
    )
