# Hosted API Reference

The Web Speed hosted API gives your agents access to a shared, growing map registry — every URL fetched by any agent anywhere is stored once and served to everyone instantly on the next request. No infrastructure to run, no Playwright to install, no servers to maintain.

**Base URL:** `https://api.yourdomain.com`

---

## Authentication

Every request requires an API key in the header:

```
X-Web-Speed-Key: wsp_your_key_here
```

Keys are issued per customer. Contact us to get one.

---

## Cache behaviour

Every response includes an `X-Cache` header:

| Value | Meaning |
|---|---|
| `HIT` | Returned from the shared registry — instant response |
| `MISS` | Fetched live from the web, stored for future requests |

The first agent to visit a URL pays the fetch cost. Every agent after that gets `X-Cache: HIT` in milliseconds. The registry grows passively with normal usage.

---

## Endpoints

### `POST /v1/map` — Interpret a page

Returns a full structured map for any URL.

**Request**

```json
{
  "url": "https://example.com",
  "js": false,
  "wait_for": null
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `url` | string | required | The URL to fetch |
| `js` | boolean | `false` | Run in headless Chromium (for React/Vue/Angular pages) |
| `wait_for` | string | `null` | CSS selector to wait for before capturing the DOM |

**Response**

```json
{
  "url": "https://example.com",
  "fetched_at": "2026-01-01T12:00:00Z",
  "page_type": "listing",
  "title": "Example",
  "description": "...",
  "headings": [{ "level": 1, "text": "Example Domain" }],
  "navigation": [{ "label": "Home", "url": "https://example.com", "location": "header" }],
  "content_links": {
    "total": 47,
    "truncated": false,
    "items": [{ "label": "Read more", "url": "https://example.com/article/1" }]
  },
  "forms": [{
    "id": "search",
    "action": "https://example.com/search",
    "method": "GET",
    "fields": [
      { "name": "q", "type": "text", "label": "Search", "placeholder": "...", "required": false, "value": "" }
    ]
  }],
  "tables": [{ "headers": ["Name", "Price"], "rows": [["Widget A", "$9.99"]] }],
  "text_blocks": [{ "tag": "p", "text": "This domain is for examples." }],
  "metadata": { "lang": "en", "canonical": "", "open_graph": { "title": "", "description": "", "image": "" } },
  "auth_gated": false,
  "js_rendered": false,
  "js_required": false
}
```

**Example — curl**

```bash
curl -X POST https://api.yourdomain.com/v1/map \
  -H "X-Web-Speed-Key: wsp_your_key_here" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://news.ycombinator.com"}'
```

**Example — Python**

```python
import httpx

client = httpx.Client(
    base_url="https://api.yourdomain.com",
    headers={"X-Web-Speed-Key": "wsp_your_key_here"},
)

resp = client.post("/v1/map", json={"url": "https://news.ycombinator.com"})
page = resp.json()

print(page["page_type"])      # "listing"
print(page["title"])          # "Hacker News"
print(resp.headers["x-cache"]) # "HIT" or "MISS"

for link in page["content_links"]["items"]:
    print(link["label"], link["url"])
```

**Example — JavaScript**

```javascript
const resp = await fetch("https://api.yourdomain.com/v1/map", {
  method: "POST",
  headers: {
    "X-Web-Speed-Key": "wsp_your_key_here",
    "Content-Type": "application/json",
  },
  body: JSON.stringify({ url: "https://news.ycombinator.com" }),
});

const page = await resp.json();
console.log(page.page_type);           // "listing"
console.log(resp.headers.get("x-cache")); // "HIT" or "MISS"
```

---

### `POST /v1/form` — Submit a form (raw HTTP)

Submit a form without a browser. Fast and lightweight — use this for standard GET/POST forms where JavaScript is not involved.

**Request**

```json
{
  "url": "https://example.com/search",
  "method": "GET",
  "fields": {
    "q": "web scraping",
    "_csrf": "token-from-hidden-field"
  }
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `url` | string | required | The form action URL |
| `method` | string | `"GET"` | `"GET"` or `"POST"` |
| `fields` | object | `{}` | Key-value map of field names to values. Include hidden fields (CSRF tokens) verbatim. |

**Response** — same shape as `/v1/map`, for the page the server lands on after submission.

---

### `POST /v1/form-submit` — Fill and submit a form in a real browser

Use this for forms that require JavaScript: login pages with JS validation, multi-step wizards, forms that load options dynamically.

**Request**

```json
{
  "url": "https://example.com/login",
  "fields": {
    "email": "user@example.com",
    "password": "hunter2",
    "remember_me": "true"
  },
  "submit_selector": "[type=\"submit\"]"
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `url` | string | required | Page URL containing the form |
| `fields` | object | required | Field `name` attributes as keys, values to fill in as values |
| `submit_selector` | string | `[type="submit"]` | CSS selector for the submit button. Override when a page has multiple buttons. |

Field types handled automatically:
- **text, email, password, textarea** — filled with the value
- **checkbox** — `"true"` / `"1"` / `"yes"` to check, anything else to uncheck
- **radio** — pass the `value` attribute of the option to select
- **select** — pass the option value to select

**Response** — map of the page after submission (the logged-in dashboard, confirmation page, etc.)

---

### `POST /v1/click` — Click an element, return the result

Navigate to a URL, click the element matching a CSS selector, and return the map of the resulting page.

**Request**

```json
{
  "url": "https://news.ycombinator.com",
  "selector": "a.morelink",
  "wait_for": null
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `url` | string | required | Page URL |
| `selector` | string | required | CSS selector for the element to click |
| `wait_for` | string | `null` | CSS selector to wait for after clicking |

**Response** — map of the page after the click resolves.

---

### `POST /v1/site-map` — Crawl a domain

Crawl from a root URL and return a combined map of all pages discovered, staying on the same domain.

**Request**

```json
{
  "root_url": "https://docs.example.com",
  "max_pages": 25,
  "js": false
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `root_url` | string | required | Starting URL for the crawl |
| `max_pages` | integer | `25` | Maximum pages to crawl |
| `js` | boolean | `false` | Use Chromium for each page (slower but handles JS) |

**Response**

```json
{
  "root_url": "https://docs.example.com",
  "crawled_at": "2026-01-01T12:00:00Z",
  "total_pages": 12,
  "pages": [
    {
      "url": "https://docs.example.com",
      "title": "Introduction",
      "page_type": "article",
      "depth": 0,
      "links_to": ["https://docs.example.com/quickstart", "https://docs.example.com/api"]
    }
  ],
  "all_forms": [
    {
      "found_on": "https://docs.example.com/contact",
      "id": "contact",
      "action": "https://docs.example.com/contact/submit",
      "method": "POST",
      "fields": [...]
    }
  ],
  "all_navigation": [
    { "label": "Quickstart", "url": "https://docs.example.com/quickstart" }
  ]
}
```

---

### `GET /v1/health` — Liveness check

No authentication required.

```json
{ "status": "ok", "version": "1.0.0", "playwright": true }
```

---

### `DELETE /v1/registry?url=…` — Invalidate a cached URL

Forces the next request for this URL to fetch fresh from the web.

```bash
curl -X DELETE "https://api.yourdomain.com/v1/registry?url=https://example.com" \
  -H "X-Web-Speed-Key: wsp_your_key_here"
```

```json
{ "url": "https://example.com", "invalidated": true }
```

---

## Error responses

When a tool fails, it returns a JSON error body (never an exception):

```json
{
  "error": true,
  "code": "TIMEOUT",
  "message": "browser timeout loading https://example.com",
  "url": "https://example.com"
}
```

| Code | Meaning |
|---|---|
| `FETCH_FAILED` | Network error — DNS failure, connection refused, etc. |
| `TIMEOUT` | The page took too long to respond (10s for httpx, 30s for Playwright) |
| `NOT_HTML` | The URL returned a non-HTML response (PDF, image, JSON, etc.) |
| `BLOCKED` | The URL targets a private or reserved IP address (SSRF protection) |
| `PARSE_FAILED` | The HTML could not be parsed |

HTTP status codes from the API itself:

| Status | Meaning |
|---|---|
| `200` | Success (even if the tool returned an error JSON body) |
| `401` | Missing or invalid API key |
| `422` | Malformed request body |
| `500` | Internal server error |

---

## Recipes

### Read a page and follow a link

```python
# Get the listing
page = client.post("/v1/map", json={"url": "https://news.ycombinator.com"}).json()

# Pick the first story
first_link = page["content_links"]["items"][0]

# Read the article
article = client.post("/v1/map", json={"url": first_link["url"]}).json()
print(article["title"])
print(article["text_blocks"][0]["text"])
```

### Log in to a site and read the dashboard

```python
# Step 1 — get the login form (find field names + CSRF token)
login_page = client.post("/v1/map", json={"url": "https://app.example.com/login"}).json()
csrf = next(f["value"] for form in login_page["forms"]
            for f in form["fields"] if f["type"] == "hidden")

# Step 2 — submit credentials
dashboard = client.post("/v1/form-submit", json={
    "url": "https://app.example.com/login",
    "fields": {"email": "user@example.com", "password": "••••", "_csrf": csrf}
}).json()

print(dashboard["page_type"])  # "listing" or "other" — you're in
```

### Plan a multi-step workflow before starting

```python
# Crawl the entire app to understand the structure
site = client.post("/v1/site-map", json={
    "root_url": "https://app.example.com",
    "max_pages": 50
}).json()

# Find every form across the site
for form in site["all_forms"]:
    print(form["found_on"], "→", form["action"], form["method"])
```

### Inspect product cards on a listing page

```python
resp = client.get("/v1/inspect", params={
    "url": "https://shop.example.com/products",
    "selector": ".product-card"
})
cards = resp.json()

for card in cards["elements"]:
    print(card["text"])    # "Widget Pro $49.99"
    print(card["links"])   # [{"label": "Add to cart", "url": "..."}]
```

---

## Limits by plan

| | Free | Scale | Enterprise |
|---|---|---|---|
| Maps / month | 500 | Unlimited (usage-based) | Unlimited |
| JS rendering (`js=true`) | ✓ | ✓ | ✓ |
| Registry access (cache HITs) | ✓ | ✓ | ✓ |
| Price | — | $0.01 / map (MISS) · $0.002 / map (HIT) | Custom |
| SLA | — | 99.5% | 99.9% + priority support |

Contact us to get a key or upgrade a plan.
