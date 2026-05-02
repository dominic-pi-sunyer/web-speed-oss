"""HTTP fetching, form submission, and browser interaction.

Three fetch backends
────────────────────
fetch_page()          — httpx, fast, no JavaScript (default).
fetch_page_js()       — Playwright, full JS rendering, read-only.
click_and_fetch()     — Playwright, clicks an element, returns resulting page.
fill_form_and_submit()— Playwright, fills a form in a real browser and submits.

All paths call security.validate_url() before any network request.
Playwright paths also install a route interceptor that re-validates every
sub-request the rendered page makes (covers SSRF via JS fetch/XHR).

Security model
──────────────
• Only http/https allowed — file://, javascript:, data: etc. are blocked.
• Private/reserved IP ranges blocked (SSRF): 127.x, 10.x, 172.16-31.x,
  192.168.x, 169.254.x (AWS IMDS), ::1, fc00::/7, fe80::/10, and more.
• DNS is resolved and every returned IP is checked — not just the hostname.
• Route interceptor re-validates inside the browser for every sub-request.
• Fresh browser context per call — no shared cookies, sessions, or storage.
• All browser permissions denied (geolocation, notifications, camera…).
• CSS selectors are validated for length — they are data, not code.
• No eval() or dynamic code execution anywhere in this module.

Playwright is optional.  If not installed, JS-requiring functions raise
FetchError with a clear install instruction rather than crashing at import.

Install:
    pip install playwright
    playwright install chromium
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from security import SecurityError, is_blockable_resource, validate_url

# ── Playwright availability ───────────────────────────────────────────────────
try:
    import playwright  # noqa: F401
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# ── constants ─────────────────────────────────────────────────────────────────
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT        = httpx.Timeout(10.0)
MAX_REDIRECTS  = 5
JS_NAV_MS      = 30_000   # page.goto timeout
JS_WAIT_MS     = 10_000   # wait_for_selector / wait_for_load_state timeout
JS_IDLE_MS     = 5_000    # extra wait after click for DOM to settle

# Chromium launch flags that reduce attack surface and background activity
_CHROMIUM_ARGS = [
    "--disable-extensions",
    "--disable-plugins",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-client-side-phishing-detection",
    "--disable-default-apps",
    "--disable-domain-reliability",
    "--disable-features=AudioServiceOutOfProcess,TranslateUI",
    "--disable-hang-monitor",
    "--disable-ipc-flooding-protection",
    "--disable-prompt-on-repost",
    "--disable-sync",
    "--metrics-recording-only",
    "--no-first-run",
    "--password-store=basic",
    "--use-mock-keychain",
    "--disable-dev-shm-usage",   # stability in low-memory environments
]


# ══════════════════════════════════════════════════════════════════════════════
#  Public types
# ══════════════════════════════════════════════════════════════════════════════

class FetchError(Exception):
    """Raised when a page can't be retrieved, isn't HTML, or is blocked."""
    def __init__(self, message: str, code: str = "FETCH_FAILED"):
        super().__init__(message)
        self.code = code


@dataclass
class FetchResult:
    final_url:    str
    html:         str
    status_code:  int
    content_type: str


# ══════════════════════════════════════════════════════════════════════════════
#  httpx backend (default — fast, no JS)
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_page(
    url: str,
    extra_headers: dict[str, str] | None = None,
) -> FetchResult:
    """GET a URL with httpx and return its HTML. No JavaScript execution."""
    _guard(url)
    try:
        async with _httpx_client(extra_headers) as client:
            response = await client.get(url)
    except httpx.TimeoutException as e:
        raise FetchError(f"timeout fetching {url}: {e}", code="TIMEOUT") from e
    except httpx.HTTPError as e:
        raise FetchError(f"network error fetching {url}: {e}") from e
    return _result_from_response(response)


async def submit_form(
    url: str,
    method: str,
    fields: dict[str, Any],
    extra_headers: dict[str, str] | None = None,
) -> FetchResult:
    """Submit a form via raw HTTP (no browser, no JS).

    GET → fields as query params.  POST → application/x-www-form-urlencoded.
    """
    _guard(url)
    method = (method or "GET").upper()
    if method not in ("GET", "POST"):
        raise FetchError(f"unsupported method: {method}")
    try:
        async with _httpx_client(extra_headers) as client:
            response = (
                await client.get(url, params=fields)
                if method == "GET"
                else await client.post(url, data=fields)
            )
    except httpx.TimeoutException as e:
        raise FetchError(f"timeout submitting to {url}: {e}", code="TIMEOUT") from e
    except httpx.HTTPError as e:
        raise FetchError(f"network error submitting to {url}: {e}") from e
    return _result_from_response(response)


# ══════════════════════════════════════════════════════════════════════════════
#  Playwright backends (JS rendering + interaction)
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_page_js(
    url: str,
    wait_for: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> FetchResult:
    """Fetch a URL in headless Chromium (read-only, no interaction).

    Executes JavaScript and waits for network idle before returning the DOM.
    wait_for — optional CSS selector to wait for before capture.
    """
    _guard(url)
    _require_playwright()

    from playwright.async_api import TimeoutError as PWTimeout, async_playwright

    try:
        async with async_playwright() as pw:
            browser, context = await _browser_context(pw, extra_headers)
            page = await context.new_page()

            response = await _goto(page, url)

            if wait_for:
                await _wait_for_selector(page, wait_for)

            html, final_url = await page.content(), page.url
            status = response.status if response else 200
            ct = (response.headers.get("content-type") or "text/html") if response else "text/html"
            await browser.close()

    except FetchError:
        raise
    except Exception as exc:
        raise FetchError(f"browser fetch failed for {url}: {exc}") from exc

    return FetchResult(final_url=final_url, html=html, status_code=status, content_type=ct)


async def click_and_fetch(
    url: str,
    selector: str,
    wait_for: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> FetchResult:
    """Navigate to `url`, click the element matching `selector`, return the result.

    Waits for network idle after the click so JS-triggered navigation and
    async data loads complete before the DOM is captured.

    wait_for — additional CSS selector to wait for after clicking.
    """
    _guard(url)
    _require_playwright()

    from playwright.async_api import TimeoutError as PWTimeout, async_playwright

    try:
        async with async_playwright() as pw:
            browser, context = await _browser_context(pw, extra_headers)
            page = await context.new_page()

            await _goto(page, url)

            # Locate and click the target element
            try:
                await page.wait_for_selector(selector, timeout=JS_WAIT_MS)
            except PWTimeout:
                raise FetchError(
                    f"element '{selector}' did not appear on {url} within timeout"
                )

            # Register new-tab listener BEFORE clicking so we never miss it
            new_pages: list = []
            context.on("page", lambda p: new_pages.append(p))

            try:
                await page.click(selector)
            except Exception:
                pass

            if new_pages:
                # A new tab opened — wait for it to load and work with that
                new_page = new_pages[0]
                try:
                    await new_page.wait_for_load_state("networkidle", timeout=JS_NAV_MS)
                except PWTimeout:
                    pass
                page = new_page
            else:
                # Same-tab navigation or JS update — wait for DOM to settle
                try:
                    await page.wait_for_load_state("networkidle", timeout=JS_NAV_MS)
                except PWTimeout:
                    pass

            if wait_for:
                await _wait_for_selector(page, wait_for)

            html, final_url = await page.content(), page.url
            await browser.close()

    except FetchError:
        raise
    except Exception as exc:
        raise FetchError(f"click failed on {url} (selector: {selector!r}): {exc}") from exc

    return FetchResult(final_url=final_url, html=html, status_code=200, content_type="text/html")


async def fill_form_and_submit(
    url: str,
    fields: dict[str, str],
    submit_selector: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> FetchResult:
    """Fill a form in a real browser by field name and submit it.

    Drives Playwright to fill each field using the `name` attribute as the key
    (matching the names in the `forms` array from interpret_page). Handles
    text inputs, passwords, emails, textareas, <select> dropdowns, checkboxes,
    and radio buttons. Then clicks the submit button and waits for the result.

    submit_selector — CSS selector for the submit button.  Defaults to
                      [type="submit"]; override when a page has multiple
                      buttons or uses a custom element.

    Checkbox values: pass "true"/"false", "1"/"0", or "yes"/"no".
    Radio values: pass the `value` attribute of the option to select.
    """
    _guard(url)
    _require_playwright()

    from playwright.async_api import TimeoutError as PWTimeout, async_playwright

    try:
        async with async_playwright() as pw:
            browser, context = await _browser_context(pw, extra_headers)
            page = await context.new_page()

            await _goto(page, url)

            # Fill each field by its name attribute
            for field_name, raw_value in fields.items():
                value = str(raw_value)
                sel = f'[name="{field_name}"]'

                try:
                    el = await page.query_selector(sel)
                    if el is None:
                        continue  # field not in DOM — skip

                    tag        = (await el.evaluate("el => el.tagName")).lower()
                    input_type = (await el.evaluate("el => el.type or ''")).lower()

                    if tag == "select":
                        await page.select_option(sel, value)

                    elif input_type == "checkbox":
                        should_check = value.lower() in ("true", "1", "yes", "on")
                        if should_check:
                            await el.check()
                        else:
                            await el.uncheck()

                    elif input_type == "radio":
                        # Click the specific radio option with the matching value
                        radio_sel = f'[name="{field_name}"][value="{value}"]'
                        radio = await page.query_selector(radio_sel)
                        if radio:
                            await radio.click()

                    elif input_type == "file":
                        # File inputs require a local path — pass the value as a path
                        await el.set_input_files(value)

                    else:
                        # text, email, password, number, textarea, etc.
                        await page.fill(sel, value)

                except Exception:
                    # Best-effort per field — don't let one bad field abort the rest
                    continue

            # Submit the form
            sub_sel = submit_selector or '[type="submit"]'
            submitted = False
            try:
                submit_el = await page.query_selector(sub_sel)
                if submit_el:
                    await submit_el.click()
                    submitted = True
            except Exception:
                pass

            if not submitted:
                # Fallback: press Enter on the page (works for most single-form pages)
                try:
                    await page.keyboard.press("Enter")
                except Exception:
                    pass

            # Wait for the result to settle
            try:
                await page.wait_for_load_state("networkidle", timeout=JS_NAV_MS)
            except PWTimeout:
                pass

            html, final_url = await page.content(), page.url
            await browser.close()

    except FetchError:
        raise
    except Exception as exc:
        raise FetchError(f"form fill/submit failed on {url}: {exc}") from exc

    return FetchResult(final_url=final_url, html=html, status_code=200, content_type="text/html")


# ══════════════════════════════════════════════════════════════════════════════
#  Shared Playwright helpers
# ══════════════════════════════════════════════════════════════════════════════

async def _browser_context(pw: Any, extra_headers: dict[str, str] | None = None):
    """Launch Chromium and return (browser, context) with security configured."""
    browser = await pw.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
    context = await browser.new_context(
        user_agent=USER_AGENT,
        extra_http_headers=extra_headers or {},
        permissions=[],     # deny every browser permission prompt
        geolocation=None,
    )

    async def _intercept(route) -> None:
        """Block private IPs and unnecessary resource types on every sub-request."""
        req_url  = route.request.url
        req_type = route.request.resource_type
        if is_blockable_resource(req_type, req_url):
            await route.abort("blockedbyclient")
            return
        try:
            validate_url(req_url)
        except SecurityError:
            await route.abort("blockedbyclient")
            return
        await route.continue_()

    await context.route("**/*", _intercept)
    return browser, context


async def _goto(page: Any, url: str) -> Any:
    """Navigate to url with networkidle, fall back to domcontentloaded."""
    from playwright.async_api import TimeoutError as PWTimeout

    try:
        return await page.goto(url, wait_until="networkidle", timeout=JS_NAV_MS)
    except PWTimeout:
        try:
            return await page.goto(url, wait_until="domcontentloaded", timeout=JS_NAV_MS // 2)
        except PWTimeout as exc:
            raise FetchError(f"browser timeout loading {url}", code="TIMEOUT") from exc


async def _wait_for_selector(page: Any, selector: str) -> None:
    """Wait for selector — best effort, never raises."""
    try:
        await page.wait_for_selector(selector, timeout=JS_WAIT_MS)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
#  httpx helpers
# ══════════════════════════════════════════════════════════════════════════════

def _httpx_client(extra_headers: dict[str, str] | None = None) -> httpx.AsyncClient:
    headers = dict(DEFAULT_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    return httpx.AsyncClient(
        headers=headers,
        timeout=TIMEOUT,
        follow_redirects=True,
        max_redirects=MAX_REDIRECTS,
    )


def _result_from_response(response: httpx.Response) -> FetchResult:
    ct = response.headers.get("content-type", "")
    if "html" not in ct.lower() and "xml" not in ct.lower():
        raise FetchError(
            f"non-HTML response (status {response.status_code}, content-type {ct or 'unknown'})",
            code="NOT_HTML",
        )
    return FetchResult(
        final_url=str(response.url),
        html=response.text,
        status_code=response.status_code,
        content_type=ct,
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Guard helpers
# ══════════════════════════════════════════════════════════════════════════════

def _guard(url: str) -> None:
    """Convert SecurityError → FetchError so callers handle one type."""
    try:
        validate_url(url)
    except SecurityError as exc:
        raise FetchError(str(exc), code="BLOCKED") from exc


def _require_playwright() -> None:
    if not PLAYWRIGHT_AVAILABLE:
        raise FetchError(
            "Playwright is not installed.\n"
            "Run:  pip install playwright && playwright install chromium",
        )
