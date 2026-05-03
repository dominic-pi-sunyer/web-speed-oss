# Web Speed

Web Speed solves the **Signal-to-Noise problem** for AI agents. While the modern web is optimized for human eyes (messy HTML, complex layouts, JS-heavy interfaces), Web Speed translates that chaos into a deterministic, token-efficient structural map designed for high-throughput agentic fleets.

**No AI inside.** No `anthropic`, no `openai`, no LLM dependency of any kind. All interpretation lives in the calling agent.

---

## Why it exists

| Problem | Web Speed solution |
|---|---|
| Raw HTML is 150,000+ chars of scripts, styles, and SVG noise | Strips everything non-structural → **up to 97% token reduction** |
| LLMs hallucinate element IDs and miss interaction points in raw DOM | Returns a **frozen structural map** — what's there is there, nothing invented |
| Custom scrapers break per-site | **Deterministic protocol** — the same JSON shape for every site on the web |
| Agent has to re-discover pages one round-trip at a time | `site_map` crawls an entire domain in one call |

---

## Tools

| Tool | Description |
|---|---|
| `interpret_page` | Full structured map: headings, navigation, content links, forms, tables, text, metadata |
| `submit_form` | Submit a form (GET or POST), get back the resulting page's map |
| `site_map` | Crawl from a root URL, return a combined map of all pages |
| `inspect_element` | Deep structural data for nodes matching a CSS selector |
| `page_type` | Instant page classification — `login`, `listing`, `article`, `form`, `navigation`, `other` |
| `invalidate_cache` | Drop a cached map so the next call fetches fresh |

---

## Install

### Mac / Linux

```bash
cd web-interpreter
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Windows

```powershell
cd web-interpreter
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

---

## Run

For local development with the MCP inspector:

```bash
mcp dev server.py
```

To run directly over stdio (how MCP clients launch it):

```bash
python server.py
```

---

## Register with Claude / Cowork

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (Mac) or the equivalent on Windows:

```json
{
  "mcpServers": {
    "web-speed": {
      "command": "/absolute/path/to/web-interpreter/venv/bin/python",
      "args": ["/absolute/path/to/web-interpreter/server.py"]
    }
  }
}
```

Then quit and relaunch Claude Desktop / Cowork. The six tools will appear under the `web-speed` MCP server.

---

## Output schemas

### `interpret_page`

```json
{
  "url": "https://example.com/",
  "fetched_at": "2025-01-01T12:00:00Z",
  "page_type": "other",
  "title": "Example Domain",
  "description": "",
  "headings": [
    { "level": 1, "text": "Example Domain" }
  ],
  "navigation": [
    { "label": "Home", "url": "https://example.com/", "location": "header" }
  ],
  "content_links": {
    "total": 47,
    "truncated": false,
    "items": [
      { "label": "More information...", "url": "https://www.iana.org/domains/example" }
    ]
  },
  "forms": [
    {
      "id": "search",
      "action": "https://example.com/search",
      "method": "GET",
      "fields": [
        {
          "name": "q",
          "type": "text",
          "label": "Search",
          "placeholder": "Search...",
          "required": false,
          "value": ""
        },
        {
          "name": "_csrf",
          "type": "hidden",
          "label": "",
          "placeholder": "",
          "required": false,
          "value": "abc123"
        }
      ]
    }
  ],
  "tables": [
    {
      "id": "results",
      "headers": ["Name", "Price", "Stock"],
      "rows": [["Widget A", "$9.99", "In stock"]]
    }
  ],
  "text_blocks": [
    { "tag": "p", "text": "This domain is for use in illustrative examples." }
  ],
  "metadata": {
    "lang": "en",
    "canonical": "",
    "open_graph": { "title": "", "description": "", "image": "" }
  }
}
```

**Key fields:**

- **`navigation`** — links inside semantic nav/header/footer elements (site chrome, menus). Capped at 60.
- **`content_links`** — links inside the page body (articles, search results, listings). Always includes `total` so you know the real count even when truncated at 60.
- **`forms`** — every form with every field, CSRF tokens preserved verbatim in hidden-field `value`.
- **`page_type`** — inferred from structure: password field → `login`, many items/links → `listing`, `<article>` with paragraphs → `article`, forms → `form`, mostly links → `navigation`.

---

### `page_type`

Lightweight — returns just the classification. Instant when the page is cached.

```json
{
  "url": "https://example.com/login",
  "fetched_at": "2025-01-01T12:00:00Z",
  "page_type": "login",
  "title": "Sign In"
}
```

---

### `submit_form`

Same output shape as `interpret_page`, for the page the server lands on after submission.

```json
{
  "url": "https://example.com/login",
  "method": "POST",
  "fields": {
    "email": "user@example.com",
    "password": "hunter2",
    "_csrf": "abc123"
  }
}
```

CSRF tokens go in `fields` verbatim — pull them from the hidden fields in the `forms` array of the prior `interpret_page` call.

---

### `inspect_element`

Deep structural data for nodes matching a CSS selector. Capped at 25 elements.

```json
{
  "url": "https://example.com/shop",
  "selector": ".product-card",
  "matched": 48,
  "truncated": true,
  "elements": [
    {
      "tag": "div",
      "id": "product-42",
      "classes": ["product-card", "featured"],
      "text": "Widget Pro $49.99 Add to cart",
      "attributes": { "id": "product-42" },
      "links": [{ "label": "Add to cart", "url": "https://example.com/cart/add/42" }],
      "fields": [],
      "children": [
        { "tag": "h3", "text": "Widget Pro" },
        { "tag": "span", "text": "$49.99" },
        { "tag": "a", "text": "Add to cart", "href": "https://example.com/cart/add/42" }
      ]
    }
  ]
}
```

Example selectors: `#login-form`, `.product-card`, `table.results tbody tr`, `nav a`, `[data-testid="price"]`

---

### `site_map`

```json
{
  "root_url": "https://example.com",
  "crawled_at": "2025-01-01T12:00:00Z",
  "total_pages": 8,
  "pages": [
    {
      "url": "https://example.com",
      "title": "Home",
      "page_type": "navigation",
      "depth": 0,
      "links_to": ["https://example.com/about", "https://example.com/contact"]
    }
  ],
  "all_forms": [
    {
      "found_on": "https://example.com/contact",
      "id": "contact",
      "action": "https://example.com/contact/submit",
      "method": "POST",
      "fields": [
        { "name": "email", "type": "email", "label": "Your email", "placeholder": "", "required": true, "value": "" },
        { "name": "message", "type": "textarea", "label": "Message", "placeholder": "", "required": true, "value": "" }
      ]
    }
  ],
  "all_navigation": [
    { "label": "About", "url": "https://example.com/about" },
    { "label": "Contact", "url": "https://example.com/contact" }
  ]
}
```

---

### `invalidate_cache`

```json
{ "url": "https://example.com", "invalidated": true }
```

---

### Errors

Tools never raise. On failure:

```json
{
  "error": true,
  "code": "FETCH_FAILED | PARSE_FAILED | TIMEOUT | NOT_HTML",
  "message": "human-readable explanation",
  "url": "https://example.com/broken"
}
```

---

## How an agent should use the output

**Navigate a site:**
Read `navigation` for site chrome (menus, header, footer) and `content_links` for page body links. `content_links.total` tells you how many exist even if the list is truncated. Pick the link matching your goal and call `interpret_page`.

**Submit a form:**
Read `forms`. Each field has `name` (what to send), `type` (what data it expects), `label`/`placeholder` (what it's for), `required`, and `value`. Hidden fields (`type: "hidden"`) carry CSRF tokens — pass their `value` back verbatim. Build a flat `name → value` dict and call `submit_form`.

**Classify before committing:**
Call `page_type` first when you need to branch logic (e.g., is this a login page or a dashboard?) without paying for a full `interpret_page`.

**Drill into a component:**
Saw a table in the map but want the individual rows? A product listing but want each card's link and price? Call `inspect_element` with a CSS selector to get structured detail on those specific nodes without re-loading the whole page.

**Pre-plan a multi-step workflow:**
Call `site_map` before you start. You get every page's title, type, depth, and outgoing links, plus every form across the site — you can plan the whole workflow (find the login form, find the data entry page, find the submit endpoint) without a single round-trip.

**`page_type` is a signal, not a guarantee:**
The classification is heuristic. JS-rendered SPAs that serve empty HTML shells will often be `other` — the password field isn't in the HTML until JavaScript runs. Treat `page_type` as a fast filter, then verify against the actual `forms` and `headings`.

---

## Architecture

```
URL  ──▶  fetcher.py         (httpx: 10s timeout, 5 redirects, Chrome UA
                ▼             OR Playwright headless Chromium for js=true)
          cleaner.py         (BeautifulSoup/lxml: strip noise, split nav vs content
                ▼             links, filter layout tables, deduplicate text blocks,
          structured map      infer page_type, detect auth_gated)
                ▼
          cache.py           (24h TTL, MD5 keyed JSON files in ./cache/)
                ▼
          server.py          (FastMCP: 8 tools over stdio)
```

No AI. No interpretation. The agent is the brain.

---

## Known limitations

- **JS-rendered SPAs**: Pages that load content via JavaScript (React, Vue, Angular) return only the pre-render HTML shell. Password fields, search results, and navigation that are injected by JS will be missing. Use `inspect_element` on what's visible and pair with a browser-automation tool for SPA-heavy targets.
- **`page_type` heuristics**: Classification is structural and fast, but not infallible. A marketing page with many internal links might be `listing`; a page with an email field and no password won't be `login`.
- **Cache is local disk**: The `./cache/` directory is local. In a multi-process or distributed deployment, cache entries won't be shared across instances. For shared caching, replace `cache.py` with a Redis or Memcached backend.
- **Rate limits not enforced**: Web Speed does not throttle outbound requests. For high-volume agent fleets, put a rate-limiting proxy (e.g., Cloudflare, nginx) in front of the server.
