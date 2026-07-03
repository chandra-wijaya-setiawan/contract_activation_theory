"""web_browse capability — real-browser page extraction with curl fallback.

Backend selection (automatic):
  1. browser-use-terminal (CDP + Chrome) — when installed; handles JS-rendered
     pages, login state, dynamic content. Install:
       curl -fsSL https://browser-use.com/terminal/install.sh | sh
       browser-use-terminal browser preference use managed-headless
  2. curl — lightweight fallback for static pages / APIs.

The model calls:
  web_browse(url="https://...", extract="text")   # default
  web_browse(url="https://...", extract="screenshot")
  web_browse(url="https://...", extract="links")
  web_browse(script="new_tab(url)\\n...")          # custom CDP script
"""
from __future__ import annotations

import html
import re
import subprocess
from typing import Any

from cat.capability.registry import Capability, ToolResult
from cat.contract.model import State

_OUTPUT_CAP = 8_000

_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": "URL to open",
        },
        "extract": {
            "type": "string",
            "description": (
                "What to extract: 'text' (default — readable page text), "
                "'screenshot' (saves PNG, returns file path — vision models only), "
                "'links' (all anchor hrefs on page)"
            ),
        },
        "script": {
            "type": "string",
            "description": (
                "Custom CDP Python script for advanced interaction. "
                "Available functions: new_tab(url), wait_for_load(), page_info(), "
                "capture_screenshot(), js(code), wait_for_element(css), type_text(css, text). "
                "Only runs when browser-use-terminal is installed."
            ),
        },
    },
}


def create_web_browse_capability() -> Capability:
    return Capability(
        name="web_browse",
        description=(
            "Fetch a URL and extract text, links, or a screenshot. "
            "Uses browser-use-terminal (CDP/Chrome) when installed for JS-heavy sites; "
            "falls back to curl for plain HTTP fetches. "
            "Use this for research, web lookups, and reading any webpage."
        ),
        json_schema=_SCHEMA,
        handler=_handle,
    )


def _truncate(s: str, cap: int) -> str:
    enc = s.encode("utf-8", errors="replace")
    if len(enc) <= cap:
        return s
    return enc[:cap].decode("utf-8", errors="replace") + f"\n... [truncated at {cap} bytes]"


def _strip_html(raw: str) -> str:
    text = re.sub(r'<(script|style|nav|header|footer)[^>]*>.*?</(script|style|nav|header|footer)>',
                  '', raw, flags=re.S | re.I)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return html.unescape(text).strip()


def _has_browser_use() -> bool:
    try:
        r = subprocess.run(["which", "browser-use-terminal"],
                           capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False


def _run_browser_use(url: str, extract: str, custom_script: str) -> ToolResult:
    if custom_script:
        script = custom_script
    elif extract == "screenshot":
        script = f'new_tab("{url}")\nwait_for_load()\nprint(capture_screenshot())\n'
    elif extract == "links":
        script = (
            f'new_tab("{url}")\n'
            f'wait_for_load()\n'
            f'links = js("Array.from(document.querySelectorAll(\'a[href]\')).map(a=>a.href).join(\'\\\\n\')")\n'
            f'print(links)\n'
        )
    else:
        script = f'new_tab("{url}")\nwait_for_load()\nprint(page_info())\n'

    try:
        proc = subprocess.run(
            ["browser-use-terminal", "browser", "exec"],
            input=script,
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(tool="web_browse", is_error=True,
                          output="TIMEOUT: browser exec did not complete within 60s")
    except Exception as exc:
        return ToolResult(tool="web_browse", is_error=True,
                          output=f"browser-use-terminal error: {exc}")

    out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
    return ToolResult(
        tool="web_browse",
        is_error=(proc.returncode != 0),
        output=_truncate(out, _OUTPUT_CAP),
        detail={"backend": "browser-use-terminal", "returncode": proc.returncode, "url": url},
    )


def _extract_article(raw: str) -> str:
    """Extract article text from HTML. Uses trafilatura if available."""
    try:
        from trafilatura import extract
        text = extract(
            raw,
            include_comments=False,
            include_tables=True,
            deduplicate=True,
        )
        if text and len(text.strip()) > 100:
            return text.strip()
    except Exception:
        pass
    return _strip_html(raw)


def _run_curl(url: str, extract: str) -> ToolResult:
    cmd = [
        "curl", "-L", "-s", "--max-time", "20",
        "--user-agent",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0 Safari/537.36",
        "--header", "Accept: text/html,application/xhtml+xml,*/*;q=0.9",
        "--header", "Accept-Language: en-US,en;q=0.9",
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        return ToolResult(tool="web_browse", is_error=True, output="TIMEOUT: curl timed out after 25s")
    except Exception as exc:
        return ToolResult(tool="web_browse", is_error=True, output=f"curl error: {exc}")

    if proc.returncode != 0:
        return ToolResult(tool="web_browse", is_error=True,
                          output=f"curl exit {proc.returncode}: {(proc.stderr or '')[:400]}")

    raw = proc.stdout or ""
    if extract == "links":
        links = re.findall(r'href=["\']([^"\'#][^"\']*)["\']', raw)
        abs_links = [l for l in links if l.startswith("http")][:60]
        output = "\n".join(abs_links) or "(no absolute links found)"
    elif "<html" in raw.lower() or "<body" in raw.lower():
        output = _extract_article(raw)
    else:
        output = raw

    return ToolResult(
        tool="web_browse",
        is_error=False,
        output=_truncate(output, _OUTPUT_CAP),
        detail={"backend": "curl", "url": url, "raw_bytes": len(raw)},
    )


def _handle(args: dict[str, Any], state: State) -> ToolResult:
    url = (args.get("url") or "").strip()
    extract = str(args.get("extract") or "text").lower()
    custom_script = (args.get("script") or "").strip()

    if not url and not custom_script:
        return ToolResult(tool="web_browse", is_error=True, output="missing 'url'")

    if _has_browser_use():
        return _run_browser_use(url, extract, custom_script)

    if url and extract != "screenshot":
        return _run_curl(url, extract)

    return ToolResult(
        tool="web_browse",
        is_error=True,
        output=(
            "browser-use-terminal is not installed. For full JS-rendered browsing, install it:\n"
            "  curl -fsSL https://browser-use.com/terminal/install.sh | sh\n"
            "  browser-use-terminal browser preference use managed-headless\n\n"
            "For plain text pages, use bash(command='curl -L -s <url>') directly."
        ),
    )
