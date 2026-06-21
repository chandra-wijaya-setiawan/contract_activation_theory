"""web_search capability — structured web search using Wikipedia API.

Primary source: Wikipedia search API (always accessible, no bot detection).
Returns title + snippet + URL for each result.

For full page content, follow up with web_browse(url=...).
For JS-rendered pages or sites requiring login, use web_browse (CDP backend).
"""
from __future__ import annotations

import json
import re
import subprocess
from typing import Any

from cat.capability.registry import Capability, ToolResult
from cat.contract.model import State

_OUTPUT_CAP = 6_000

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
            "Uses Wikipedia's search API (works without browser or API key). "
            "Good for: people, places, concepts, facts. "
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


def _handle(args: dict[str, Any], state: State) -> ToolResult:
    query = (args.get("query") or "").strip()
    if not query:
        return ToolResult(tool="web_search", is_error=True, output="missing 'query'")

    n = min(int(args.get("n_results") or 5), 10)

    results, err = _wikipedia_search(query, n)

    if err and not results:
        return ToolResult(tool="web_search", is_error=True, output=err,
                          detail={"query": query})

    lines = [f'=== Web Search: "{query}" ===', ""]

    if results:
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['title']}")
            if r.get("url"):
                lines.append(f"   {r['url']}")
            if r.get("snippet"):
                lines.append(f"   {r['snippet']}")
            lines.append("")

    if not results:
        lines.append("No results found.")
        lines.append("")
        lines.append("Tip: try web_browse(url='https://en.wikipedia.org/w/index.php?search=<query>')")
    else:
        lines.append("Tip: use web_browse(url='<URL above>') to read the full page.")

    output = "\n".join(lines)
    return ToolResult(
        tool="web_search",
        is_error=False,
        output=_truncate(output, _OUTPUT_CAP),
        detail={"query": query, "n_results": len(results), "source": "wikipedia"},
    )


def _wikipedia_search(query: str, n: int) -> tuple[list[dict], str]:
    """Search Wikipedia via its free API. Returns (results, error_or_empty)."""
    import urllib.parse

    params = urllib.parse.urlencode({
        "action": "query",
        "list": "search",
        "srsearch": query,
        "format": "json",
        "srlimit": n,
        "srprop": "snippet|titlesnippet",
    })
    url = f"https://en.wikipedia.org/w/api.php?{params}"

    cmd = [
        "curl", "-s", "--max-time", "15",
        "-H", "Accept: application/json",
        "-H", "User-Agent: CAT-Agent/1.0 (research tool; contact: cat@example.com)",
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return [], "Wikipedia search timed out"
    except Exception as exc:
        return [], f"curl error: {exc}"

    if proc.returncode != 0:
        return [], f"curl exit {proc.returncode}: {(proc.stderr or '')[:200]}"

    try:
        data = json.loads(proc.stdout)
    except Exception:
        return [], f"Wikipedia API returned non-JSON: {proc.stdout[:200]}"

    hits = data.get("query", {}).get("search", [])
    results = []
    for h in hits:
        title = h.get("title", "")
        snippet = re.sub(r"<[^>]+>", "", h.get("snippet", "")).strip()
        url_slug = title.replace(" ", "_")
        article_url = f"https://en.wikipedia.org/wiki/{url_slug}"
        results.append({"title": title, "url": article_url, "snippet": snippet})

    return results, ""
