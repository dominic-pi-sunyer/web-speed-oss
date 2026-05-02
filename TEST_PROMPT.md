# web-interpreter Test Prompt for Claude Cowork

Use this prompt in Claude Cowork to test the web-interpreter MCP server end-to-end.

---

## Test 1: Fetch and interpret a simple page

```
Use interpret_page to fetch https://example.com and tell me:
1. What is the page_type?
2. How many navigation links are there?
3. List all the text_blocks.
4. What is the canonical URL?
```

**Expected:** Page should be `page_type: "other"`, 1 nav link ("Learn more..."), 2 paragraphs about the Example Domain, canonical empty.

---

## Test 2: Site crawl

```
Use site_map to crawl https://httpbin.org up to 5 pages. Then tell me:
1. How many pages were found?
2. What page_type is each page?
3. Are there any forms in all_forms?
4. List the all_navigation entries.
```

**Expected:** Should find multiple pages of different types (likely `other` and maybe `form`), some navigation links, possibly some forms.

---

## Test 3: Form submission

```
Go to https://httpbin.org/post and use interpret_page to see what's on the page.
Then use submit_form to POST the data:
  - url: https://httpbin.org/post
  - method: POST
  - fields: {"field1": "test_value", "field2": "another_value"}

What URL did you end up on? What page_type is it?
```

**Expected:** Should POST to httpbin and land on a response page (likely showing the echoed data).

---

## Test 4: Cache behavior

```
1. Call interpret_page on https://example.com
2. Call it again immediately
3. Call invalidate_cache on https://example.com
4. Call interpret_page on https://example.com again

Tell me if the fetched_at timestamps are the same on steps 1-2 (cache hit), then different on step 4 (cache miss after invalidation).
```

**Expected:** Steps 1-2 same timestamp, step 4 newer.

---

## Test 5: Complex form parsing

```
Use interpret_page to fetch https://httpbin.org/forms/post

Then tell me:
1. How many forms are there?
2. For each form, list all fields with their name, type, label, and required status.
3. What is the form action URL and method?
4. Which fields have options (select/radio)?
```

**Expected:** Should see HTML form with various input types, selects, checkboxes, etc., all properly extracted.

---

## Test 6: Error handling

```
Try to interpret_page on a URL that doesn't exist or is invalid:
  https://this-domain-definitely-does-not-exist-12345.com

What error code and message do you get?
```

**Expected:** `error: true`, `code: "FETCH_FAILED"` or `"TIMEOUT"`, human-readable message.

---

## Notes for testing

- The server caches results for 24 hours under `./cache/` (keyed by MD5 of the URL)
- All URLs in the map are absolutized (no relative links)
- Hidden form fields (type: "hidden") preserve their `value` — important for CSRF tokens
- Inline event handlers (`onclick`, `onload`, etc.) are stripped before the map is returned
- `page_type` is inferred from signals: password field → `login`, many items → `listing`, etc.
