"""web-speed MCP server.

Translates human-facing HTML into a deterministic, token-efficient structural
map for agentic AI. No LLM inside — all interpretation lives in the calling
agent.

Tools
─────
  interpret_page    → full structured map (httpx by default; js=True for React/SPA)
  submit_form       → raw HTTP form submission, return resulting page's map
  fill_and_submit   → fill a form in a real browser and submit (handles JS forms)
  click_element     → click any element in a browser, return resulting page's map
  site_map          → crawl a site, return combined map of all pages
  inspect_element   → structural data for nodes matching a CSS selector
  page_type         → instant page classification
  invalidate_cache  → drop a cached entry so the next call fetches fresh

JS rendering
────────────
Pass js=True to interpret_page or inspect_element to use a headless Chromium
browser instead of httpx. The browser executes JavaScript, waits for the SPA
data to load, then hands the rendered HTML to the same extraction pipeline.

Requires: pip install playwright && playwright install chromium

Security
────────
Every fetch path validates the URL against an SSRF blocklist before making
any network request. The Playwright path additionally installs a route
interceptor that re-validates every sub-request the page JS makes.
See security.py for the full threat model.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP

import cache
from cleaner import clean_html, inspect_element_nodes
from fetcher import (
    PLAYWRIGHT_AVAILABLE,
    FetchError,
    click_and_fetch,
    fetch_page,
    fetch_page_js,
    fill_form_and_submit,
)
from fetcher import submit_form as _http_submit
from security import validate_selector

logger = logging.getLogger("web-speed")

mcp = FastMCP("web-speed")


# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _error(code: str, message: str, url: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": True, "code": code, "message": message}
    if url is not None:
        payload["url"] = url
    return payload


def _coerce(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return "" if v is None else str(v)


def _is_js_shell(page_map: dict[str, Any]) -> bool:
    """Return True when the map looks like an empty JS app shell.

    A shell typically has a <title> (server-rendered) but no headings, no
    forms, no text, and no body links — all content is injected by JavaScript
    after load.  We surface this as a hint so the agent can decide to retry
    with js=True.
    """
    score = (
        len(page_map.get("headings", [])) * 3
        + len(page_map.get("forms", [])) * 5
        + len(page_map.get("text_blocks", [])) * 2
        + min(page_map.get("content_links", {}).get("total", 0), 10)
    )
    return score < 5


async def _build_map(
    url: str,
    js: bool = False,
    wait_for: str | None = None,
) -> dict[str, Any]:
    """Fetch, extract, and cache a page map. Returns error dict on failure.

    js=True  → headless Chromium (Playwright); result cached under url+"|js"
    js=False → httpx (default);                result cached under url
    """
    cache_key = (url + "|js") if js else url

    if not js:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    # ── fetch ─────────────────────────────────────────────────────────────────
    try:
        if js:
            fetched = await fetch_page_js(url, wait_for=wait_for)
        else:
            fetched = await fetch_page(url)
    except FetchError as e:
        return _error(e.code, str(e), url=url)

    # ── extract ───────────────────────────────────────────────────────────────
    try:
        extracted = clean_html(fetched.html, base_url=fetched.final_url)
    except Exception as e:
        return _error("PARSE_FAILED", f"failed to parse HTML: {e}", url=fetched.final_url)

    page_map: dict[str, Any] = {
        "url":         fetched.final_url,
        "fetched_at":  _now_iso(),
        "js_rendered": js,
        **extracted,
    }

    # ── hint when the page looks like a JS shell ──────────────────────────────
    if not js and _is_js_shell(page_map):
        page_map["js_required"] = True
        page_map["js_hint"] = (
            "This page appears to be a JavaScript-rendered app (React / Vue / Angular). "
            "The map above reflects only the static HTML shell. "
            "Re-call with js=True to run the page in a headless browser and get the "
            "fully-rendered content."
            + (
                ""
                if PLAYWRIGHT_AVAILABLE
                else " NOTE: Playwright is not installed — "
                     "run: pip install playwright && playwright install chromium"
            )
        )
    else:
        page_map["js_required"] = False

    # ── cache ─────────────────────────────────────────────────────────────────
    try:
        cache.put(cache_key, page_map)
        final = fetched.final_url
        if not js and final != url:
            cache.put(final, page_map)
    except OSError as e:
        logger.warning("cache write failed for %s: %s", url, e)

    return page_map


# ══════════════════════════════════════════════════════════════════════════════
#  Tools
# ══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def interpret_page(
    url: str,
    js: bool = False,
    wait_for: str | None = None,
) -> dict[str, Any]:
    """Fetch a URL and return the full structured page map.

    The map contains: page_type, title, description, headings, navigation
    (chrome/menu links), content_links (body links with total count), forms
    (with every field, label, type, options, and CSRF-ready hidden values),
    tables, text_blocks, and metadata.

    js=False (default) — fast httpx fetch, no JavaScript execution.  Works
    for server-rendered HTML (most marketing sites, HN, Wikipedia, etc.).
    If the page is a JS app, the map will be nearly empty and the response
    will include "js_required": true as a hint.

    js=True — headless Chromium via Playwright executes the page's JavaScript,
    waits for network activity to settle, then extracts from the rendered DOM.
    Use this for React, Vue, Angular, and Next.js apps.  Slower (~3–10s vs
    ~0.5s), but returns the full rendered content.
    Requires: pip install playwright && playwright install chromium

    wait_for — CSS selector to wait for before capturing (only used with
    js=True).  Useful when a specific element signals data has loaded, e.g.
    ".search-results" or "#product-grid".  Must be a valid CSS selector
    (not JavaScript).

    Results are cached 24 hours (js=True cached separately from js=False).
    Use invalidate_cache to force a fresh fetch.
    """
    if not url or not isinstance(url, str):
        return _error("FETCH_FAILED", "missing or invalid 'url'")
    if wait_for is not None:
        try:
            validate_selector(wait_for)
        except Exception as e:
            return _error("FETCH_FAILED", f"invalid wait_for selector: {e}")
    return await _build_map(url, js=js, wait_for=wait_for)


@mcp.tool(name="submit_form")
async def submit_form(
    url: str,
    method: str,
    fields: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Submit a form and return the structured map of the resulting page.

    CSRF tokens are handled automatically — pass hidden-field values back
    verbatim from the forms array of a prior interpret_page call.
    POST sends application/x-www-form-urlencoded; GET appends query params.

    Note: if the response page is a JS-rendered SPA, the map will include
    "js_required": true.  In that case call interpret_page with js=True on
    the response URL to get the rendered content.
    """
    if not url or not isinstance(url, str):
        return _error("FETCH_FAILED", "missing or invalid 'url'")
    if not isinstance(fields, dict):
        return _error("FETCH_FAILED", "'fields' must be an object")

    str_fields = {k: _coerce(v) for k, v in fields.items()}

    try:
        fetched = await _http_submit(url, method, str_fields, extra_headers=headers)
    except FetchError as e:
        return _error(e.code, str(e), url=url)

    try:
        extracted = clean_html(fetched.html, base_url=fetched.final_url)
    except Exception as e:
        return _error("PARSE_FAILED", f"failed to parse HTML: {e}", url=fetched.final_url)

    page_map: dict[str, Any] = {
        "url":         fetched.final_url,
        "fetched_at":  _now_iso(),
        "js_rendered": False,
        **extracted,
    }
    if _is_js_shell(page_map):
        page_map["js_required"] = True
        page_map["js_hint"] = (
            "The response page appears JS-rendered. "
            "Call interpret_page with js=True on the response URL to see its content."
        )
    else:
        page_map["js_required"] = False

    try:
        cache.put(fetched.final_url, page_map)
    except OSError as e:
        logger.warning("cache write failed for %s: %s", fetched.final_url, e)

    return page_map


@mcp.tool()
async def site_map(
    root_url: str,
    max_pages: int = 10,
    stay_on_domain: bool = True,
) -> dict[str, Any]:
    """Crawl a site from a root URL and return a combined structural map.

    Crawls breadth-first up to max_pages pages following navigation chrome
    links. With stay_on_domain=True (default), off-domain links are skipped.
    Every page is cached individually.

    Note: site_map uses httpx (no JS rendering). For JS-heavy sites, use
    interpret_page with js=True on individual pages after discovering them.
    """
    if not root_url or not isinstance(root_url, str):
        return _error("FETCH_FAILED", "missing or invalid 'root_url'")
    try:
        max_pages = max(1, int(max_pages))
    except (TypeError, ValueError):
        return _error("FETCH_FAILED", "'max_pages' must be an integer")

    root_domain = urlparse(root_url).netloc
    visited: set[str]              = set()
    pages:   list[dict[str, Any]]  = []
    depths:  dict[str, int]        = {}
    queue:   deque[tuple[str, int]] = deque([(root_url, 0)])

    while queue and len(pages) < max_pages:
        url, depth = queue.popleft()
        if url in visited:
            continue
        visited.add(url)

        page_map = await _build_map(url)
        if page_map.get("error"):
            logger.info("site_map: skipping %s (%s)", url, page_map.get("code"))
            continue

        final_url = page_map.get("url", url)
        depths[final_url] = depth
        pages.append(page_map)

        if len(pages) >= max_pages:
            break

        for nav in page_map.get("navigation") or []:
            link = nav.get("url") if isinstance(nav, dict) else None
            if not link or not isinstance(link, str):
                continue
            if stay_on_domain and urlparse(link).netloc != root_domain:
                continue
            if link not in visited:
                queue.append((link, depth + 1))

    if not pages:
        return _error("FETCH_FAILED", "no pages could be interpreted", url=root_url)

    summaries: list[dict[str, Any]] = []
    all_forms: list[dict[str, Any]] = []
    nav_seen:  set[tuple[str, str]] = set()
    all_nav:   list[dict[str, Any]] = []

    for page in pages:
        page_url = page.get("url", "")
        summaries.append({
            "url":         page_url,
            "title":       page.get("title", ""),
            "page_type":   page.get("page_type", "other"),
            "js_required": page.get("js_required", False),
            "depth":       depths.get(page_url, 0),
            "links_to":    [n["url"] for n in page.get("navigation", []) if n.get("url")],
        })
        for form in page.get("forms", []):
            all_forms.append({
                "found_on": page_url,
                "id":       form.get("id"),
                "action":   form.get("action"),
                "method":   form.get("method"),
                "fields":   form.get("fields", []),
            })
        for nav in page.get("navigation", []):
            key = (nav.get("label", ""), nav.get("url", ""))
            if key in nav_seen:
                continue
            nav_seen.add(key)
            all_nav.append({"label": nav.get("label", ""), "url": nav.get("url", "")})

    summaries.sort(key=lambda p: (p["depth"], p["url"]))

    return {
        "root_url":       root_url,
        "crawled_at":     _now_iso(),
        "total_pages":    len(summaries),
        "pages":          summaries,
        "all_forms":      all_forms,
        "all_navigation": all_nav,
    }


@mcp.tool()
async def inspect_element(
    url: str,
    selector: str,
    js: bool = False,
) -> dict[str, Any]:
    """Return deep structural data for nodes matching a CSS selector.

    Returns up to 25 matched elements, each with: tag, id, classes, text,
    attributes, links within, form fields within, and immediate children.
    Use this to drill into a specific component without a full page map.

    js=True — renders the page with Playwright before running the selector.
    Essential for components that React/Vue inject into the DOM after load.

    Example selectors:
      #login-form       — element with id "login-form"
      .product-card     — elements with class "product-card"
      table.results tr  — rows inside a results table
      [data-testid="price"]  — element with a data attribute
    """
    if not url or not isinstance(url, str):
        return _error("FETCH_FAILED", "missing or invalid 'url'")
    if not selector or not isinstance(selector, str):
        return _error("FETCH_FAILED", "missing or invalid 'selector'")
    try:
        validate_selector(selector)
    except Exception as e:
        return _error("FETCH_FAILED", f"invalid selector: {e}")

    try:
        if js:
            fetched = await fetch_page_js(url)
        else:
            fetched = await fetch_page(url)
    except FetchError as e:
        return _error(e.code, str(e), url=url)

    result = inspect_element_nodes(
        fetched.html, base_url=fetched.final_url, selector=selector
    )
    result["url"] = fetched.final_url
    result["js_rendered"] = js
    return result


@mcp.tool()
async def page_type(url: str) -> dict[str, Any]:
    """Return the page classification for a URL — instant when cached.

    Returns {url, fetched_at, page_type, title, js_required}.
    Types: login, listing, article, form, navigation, other.

    js_required=true means the page is a JS shell and interpret_page with
    js=True is needed to see the actual content.
    """
    if not url or not isinstance(url, str):
        return _error("FETCH_FAILED", "missing or invalid 'url'")

    page_map = await _build_map(url)
    if page_map.get("error"):
        return page_map

    return {
        "url":         page_map.get("url", url),
        "fetched_at":  page_map.get("fetched_at", ""),
        "page_type":   page_map.get("page_type", "other"),
        "title":       page_map.get("title", ""),
        "js_required": page_map.get("js_required", False),
    }


@mcp.tool()
async def click_element(
    url: str,
    selector: str,
    wait_for: str | None = None,
) -> dict[str, Any]:
    """Click an element on a page and return the structured map of the result.

    Always uses a headless browser. Navigates to `url`, waits for `selector`
    to appear, clicks it, then waits for the page to settle and returns the
    full structured map of wherever the click leads.

    Use this for:
    - "Load more" / "Next page" pagination buttons
    - Tab or accordion panels that reveal hidden content
    - React Router / Vue Router links (no real HTTP navigation)
    - Cookie banners and modal dismissals before scraping the real content
    - Any button that fires JavaScript instead of a standard <a> or form submit

    wait_for — CSS selector to wait for after clicking, e.g. ".results-loaded".
    Useful when the click triggers async data fetching before content appears.

    The result includes js_rendered: true and the standard map fields.
    Requires: pip install playwright && playwright install chromium
    """
    if not url or not isinstance(url, str):
        return _error("FETCH_FAILED", "missing or invalid 'url'")
    if not selector or not isinstance(selector, str):
        return _error("FETCH_FAILED", "missing or invalid 'selector'")
    try:
        validate_selector(selector)
    except Exception as e:
        return _error("FETCH_FAILED", f"invalid selector: {e}")
    if wait_for is not None:
        try:
            validate_selector(wait_for)
        except Exception as e:
            return _error("FETCH_FAILED", f"invalid wait_for selector: {e}")

    try:
        fetched = await click_and_fetch(url, selector, wait_for=wait_for)
    except FetchError as e:
        return _error(e.code, str(e), url=url)

    try:
        extracted = clean_html(fetched.html, base_url=fetched.final_url)
    except Exception as e:
        return _error("PARSE_FAILED", f"failed to parse HTML: {e}", url=fetched.final_url)

    page_map: dict[str, Any] = {
        "url":          fetched.final_url,
        "fetched_at":   _now_iso(),
        "js_rendered":  True,
        "clicked":      selector,
        **extracted,
    }
    page_map["js_required"] = _is_js_shell(page_map)

    try:
        cache.put(fetched.final_url + "|js", page_map)
    except OSError as e:
        logger.warning("cache write failed for %s: %s", fetched.final_url, e)

    return page_map


@mcp.tool()
async def fill_and_submit(
    url: str,
    fields: dict[str, Any],
    submit_selector: str | None = None,
) -> dict[str, Any]:
    """Fill a form in a real browser and submit it. Returns the resulting page map.

    Unlike submit_form (which fires a raw HTTP request), this drives an actual
    browser — the page's JavaScript runs, field validation fires, dynamic
    show/hide logic works, and the submit click happens exactly as a human
    would do it.

    Use this for:
    - React / Angular / Vue forms that validate or transform fields via JS
    - Multi-step wizards where each step renders new fields
    - Login pages that use custom JS submission (not a plain HTML form POST)
    - Forms with dynamic dropdowns that load options via API after selection

    fields — {field_name: value} matching the `name` attribute from the forms
    array in an interpret_page(js=True) call. Supported types:
      text / email / password / number / textarea → fills the value
      select → selects the matching option by value
      checkbox → "true"/"1"/"yes" checks it; anything else unchecks
      radio → pass the `value` of the radio option to select

    submit_selector — CSS selector for the submit button. Default: [type="submit"].
    Override when the page has multiple buttons or a custom submit element.

    Requires: pip install playwright && playwright install chromium
    """
    if not url or not isinstance(url, str):
        return _error("FETCH_FAILED", "missing or invalid 'url'")
    if not isinstance(fields, dict):
        return _error("FETCH_FAILED", "'fields' must be an object")
    if submit_selector is not None:
        try:
            validate_selector(submit_selector)
        except Exception as e:
            return _error("FETCH_FAILED", f"invalid submit_selector: {e}")

    str_fields = {k: _coerce(v) for k, v in fields.items()}

    try:
        fetched = await fill_form_and_submit(
            url, str_fields, submit_selector=submit_selector
        )
    except FetchError as e:
        return _error(e.code, str(e), url=url)

    try:
        extracted = clean_html(fetched.html, base_url=fetched.final_url)
    except Exception as e:
        return _error("PARSE_FAILED", f"failed to parse HTML: {e}", url=fetched.final_url)

    page_map: dict[str, Any] = {
        "url":         fetched.final_url,
        "fetched_at":  _now_iso(),
        "js_rendered": True,
        **extracted,
    }
    if _is_js_shell(page_map):
        page_map["js_required"] = True
        page_map["js_hint"] = (
            "The result page appears JS-rendered. "
            "Call interpret_page with js=True on the response URL to see its content."
        )
    else:
        page_map["js_required"] = False

    try:
        cache.put(fetched.final_url + "|js", page_map)
    except OSError as e:
        logger.warning("cache write failed for %s: %s", fetched.final_url, e)

    return page_map


@mcp.tool()
async def invalidate_cache(url: str) -> dict[str, Any]:
    """Delete cached maps for a URL (both httpx and JS-rendered versions).

    Use when you know a page has changed — after a deploy, a form submission,
    or when a previous map looks stale.
    """
    if not url or not isinstance(url, str):
        return _error("FETCH_FAILED", "missing or invalid 'url'")
    removed_static = cache.invalidate(url)
    removed_js     = cache.invalidate(url + "|js")
    return {
        "url":              url,
        "invalidated":      removed_static or removed_js,
        "static_removed":   removed_static,
        "js_removed":       removed_js,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    mcp.run()


if __name__ == "__main__":
    main()
