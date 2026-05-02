# Community Edition — Setup Guide

Web Speed is an open-source MCP server that translates any webpage into a clean, structured JSON map your AI agent can actually read. No AI inside — all interpretation lives in your agent.

**What you get:** 8 tools over the Model Context Protocol, a 24-hour local cache, full JavaScript rendering via Playwright, and SSRF protection out of the box.

---

## Requirements

- Python 3.11 or later
- pip
- Claude Desktop, Cursor, or any MCP-compatible client

---

## Install

```bash
git clone https://github.com/Dominic-Pi-Sunyer/web-speed-oss
cd web-speed
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

Playwright is optional — it enables `js=true` for React/Vue/Angular pages. If you skip it, the 7 non-JS tools still work.

---

## Connect to Claude Desktop

Open `~/Library/Application Support/Claude/claude_desktop_config.json` (Mac) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows) and add:

```json
{
  "mcpServers": {
    "web-speed": {
      "command": "/absolute/path/to/web-speed/venv/bin/python",
      "args": ["/absolute/path/to/web-speed/server.py"]
    }
  }
}
```

Replace both paths with the actual location on your machine. Quit and relaunch Claude Desktop — the 8 tools appear under **web-speed** in the tool panel.

### Other MCP clients

Any client that speaks MCP over stdio works. The server command is:

```bash
/path/to/venv/bin/python /path/to/server.py
```

---

## Tools

| Tool | What it does |
|---|---|
| `interpret_page` | Full structured map: headings, links, forms, tables, text, metadata |
| `submit_form` | Submit a form via raw HTTP (GET or POST), get back the result page's map |
| `fill_and_submit` | Fill a form in a real browser and submit — handles JS-rendered forms |
| `click_element` | Click any element in a browser, return the resulting page's map |
| `site_map` | Crawl an entire domain, return a combined map of all pages |
| `inspect_element` | Deep structural detail for nodes matching a CSS selector |
| `page_type` | Instant classification: `login`, `listing`, `article`, `form`, `navigation`, `other` |
| `invalidate_cache` | Drop a cached entry so the next call fetches fresh |

---

## Usage examples

### Read any page

```
interpret_page("https://news.ycombinator.com")
```

Returns the full map — headings, top story links, navigation, forms — in a few hundred tokens instead of 200k characters of raw HTML.

### Handle React / Vue / Angular pages

```
interpret_page("https://app.example.com/dashboard", js=True)
```

Runs the page in headless Chromium, waits for JavaScript to settle, then extracts the rendered content.

### Submit a form

```
# 1. Get the page and find the form
interpret_page("https://example.com/search")

# 2. Submit it
submit_form(
  url="https://example.com/search",
  method="GET",
  fields={"q": "web scraping"}
)
```

### Fill a JS form (login, checkout, etc.)

```
fill_and_submit(
  url="https://example.com/login",
  fields={"email": "user@example.com", "password": "••••••••"},
  submit_selector='[type="submit"]'
)
```

### Click a button or link

```
click_element(
  url="https://news.ycombinator.com",
  selector="a.morelink"
)
```

Returns the map of the page after the click — works for pagination, tab switches, expandable sections.

### Map an entire site

```
site_map("https://docs.example.com", max_pages=30)
```

Returns every page title, type, depth, and outgoing links — plus every form across the site. Plan a full multi-step workflow before making a single request.

### Drill into a component

```
inspect_element("https://shop.example.com", selector=".product-card")
```

Returns structured detail (tag, classes, text, links, children) for every matched element. Useful for product listings, search results, data tables.

---

## Output format

Every tool returns the same JSON shape:

```json
{
  "url": "https://example.com",
  "fetched_at": "2026-01-01T12:00:00Z",
  "page_type": "listing",
  "title": "Example",
  "description": "...",
  "headings": [{ "level": 1, "text": "..." }],
  "navigation": [{ "label": "Home", "url": "https://example.com", "location": "header" }],
  "content_links": { "total": 47, "truncated": false, "items": [{ "label": "...", "url": "..." }] },
  "forms": [{ "id": "search", "action": "...", "method": "GET", "fields": [...] }],
  "tables": [{ "headers": ["Name", "Price"], "rows": [...] }],
  "text_blocks": [{ "tag": "p", "text": "..." }],
  "metadata": { "lang": "en", "canonical": "", "open_graph": {...} },
  "auth_gated": false,
  "js_rendered": false,
  "js_required": false
}
```

On error:

```json
{
  "error": true,
  "code": "TIMEOUT",
  "message": "browser timeout loading https://example.com",
  "url": "https://example.com"
}
```

Error codes: `FETCH_FAILED`, `TIMEOUT`, `NOT_HTML`, `BLOCKED`, `PARSE_FAILED`.

---

## Tips

**`navigation` vs `content_links`**  
`navigation` is site chrome — menus, header, footer. `content_links` is the page body — articles, search results, listings. Split on purpose so your agent doesn't mistake a footer link for a result.

**CSRF tokens**  
Hidden fields in `forms` carry CSRF tokens verbatim. Pass them back in `submit_form`'s `fields` dict — the server doesn't strip or alter them.

**Cache**  
Maps are cached locally for 24 hours in `./cache/`. Call `invalidate_cache(url)` to force a fresh fetch. Speeds up repeated agent runs significantly.

**JS pages**  
If `js_required: true` is in the response, the page is a JavaScript shell. Re-call with `js=True` to render it in Chromium.

---

## Source

[github.com/your-username/web-speed](https://github.com/your-username/web-speed) — MIT licensed.
