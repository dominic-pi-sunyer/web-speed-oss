"""HTML -> structured map.

Pure BeautifulSoup extraction. No AI, no summarization. The calling agent
interprets the output; this module only extracts.

Design goals
────────────
• Signal-only output — strip scripts, styles, SVG, images, comments, and
  inline event handlers before extracting anything.
• Navigation vs content links — structural chrome (nav/header/footer) stays
  in `navigation`; body content links go to `content_links`.  Both are capped
  so a 1 000-link listing page never blows up the map.
• Text blocks filtered for substance — fragments shorter than MIN_TEXT_CHARS
  are dropped; total capped at MAX_TEXT_BLOCKS.
• Layout tables skipped — tables with no headers and many rows are almost
  certainly CSS-layout tables; they add noise, not data.
"""

from __future__ import annotations

from collections import Counter
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Comment, Tag

# ── noise tags stripped wholesale ────────────────────────────────────────────
NOISE_TAGS = (
    "script", "style", "noscript", "svg", "img", "picture",
    "canvas", "template", "iframe",
)

# ── valid <input> type values ─────────────────────────────────────────────────
INPUT_TYPES = frozenset({
    "text", "email", "password", "search", "tel", "url", "number",
    "date", "datetime-local", "time", "month", "week", "color", "range",
    "file", "checkbox", "radio", "hidden", "submit", "reset", "button", "image",
})

# ── output caps ───────────────────────────────────────────────────────────────
MAX_NAV_LINKS       = 60    # true chrome nav links (header/footer/sidebar)
MAX_CONTENT_LINKS   = 60    # inline body links
MIN_TEXT_CHARS      = 25    # drop text blocks shorter than this
MAX_TEXT_BLOCKS     = 60    # cap total text blocks returned
MAX_TABLE_ROWS      = 50    # cap rows per table
LAYOUT_TABLE_ROWS   = 15    # tables with no headers AND > this many rows → skip
MAX_INSPECT_RESULTS = 25    # cap elements returned by inspect_element_nodes


# ══════════════════════════════════════════════════════════════════════════════
#  Public API
# ══════════════════════════════════════════════════════════════════════════════

def clean_html(html: str, base_url: str) -> dict[str, Any]:
    """Parse `html` and return the full structured map.

    `base_url` should be the final post-redirect URL so relative hrefs resolve
    correctly.
    """
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    _strip_noise(soup)
    _strip_inline_handlers(soup)

    title       = _text(soup.title) if soup.title else ""
    description = _meta_content(soup, "description")
    headings    = _extract_headings(soup)
    navigation, content_links = _extract_links(soup, base_url)
    forms       = _extract_forms(soup, base_url)
    tables      = _extract_tables(soup)
    text_blocks = _extract_text_blocks(soup)
    metadata    = _extract_metadata(soup, base_url)
    page_type   = _infer_page_type(soup, forms, content_links)
    auth_gated  = _detect_auth_gated(soup, navigation, forms)

    return {
        "page_type":      page_type,
        "title":          title,
        "description":    description,
        "headings":       headings,
        "navigation":     navigation,
        "content_links":  content_links,
        "forms":          forms,
        "tables":         tables,
        "text_blocks":    text_blocks,
        "metadata":       metadata,
        "auth_gated":     auth_gated,
    }


def inspect_element_nodes(
    html: str,
    base_url: str,
    selector: str,
) -> dict[str, Any]:
    """Return structural detail for every element matching `selector`.

    Uses BeautifulSoup's CSS selector engine. Returns up to
    MAX_INSPECT_RESULTS elements; sets `truncated: true` if more matched.
    """
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    _strip_noise(soup)
    _strip_inline_handlers(soup)

    try:
        matches = soup.select(selector)
    except Exception as exc:
        return {
            "error": True,
            "code": "PARSE_FAILED",
            "message": f"invalid CSS selector: {exc}",
            "selector": selector,
        }

    total     = len(matches)
    truncated = total > MAX_INSPECT_RESULTS
    elements  = []

    for el in matches[:MAX_INSPECT_RESULTS]:
        elements.append(_describe_element(el, base_url))

    return {
        "selector":  selector,
        "matched":   total,
        "truncated": truncated,
        "elements":  elements,
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Stripping helpers
# ══════════════════════════════════════════════════════════════════════════════

def _strip_noise(soup: BeautifulSoup) -> None:
    for tag in soup(list(NOISE_TAGS)):
        tag.decompose()
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()


def _strip_inline_handlers(soup: BeautifulSoup) -> None:
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith("on"):
                del tag.attrs[attr]


# ══════════════════════════════════════════════════════════════════════════════
#  Headings
# ══════════════════════════════════════════════════════════════════════════════

def _extract_headings(soup: BeautifulSoup) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for level in range(1, 7):
        for h in soup.find_all(f"h{level}"):
            text = _text(h)
            if text:
                out.append({"level": level, "text": text})
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  Links  ─  split into navigation chrome vs body content
# ══════════════════════════════════════════════════════════════════════════════

def _extract_links(
    soup: BeautifulSoup,
    base_url: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return (navigation, content_links_block).

    `navigation` — links inside semantic nav/header/footer/aside, or inside
      elements whose class/id contain nav-related keywords.  Capped at
      MAX_NAV_LINKS; deduplicated by (label, url).

    `content_links_block` — all other links as
      {"items": [...], "total": N, "truncated": bool}.
      Items capped at MAX_CONTENT_LINKS.
    """
    nav_entries:  list[dict[str, Any]] = []
    body_entries: list[dict[str, Any]] = []
    nav_seen:  set[tuple[str, str]] = set()
    body_seen: set[tuple[str, str]] = set()

    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = urljoin(base_url, href)
        label = (_text(a)
                 or (a.get("aria-label") or "").strip()
                 or (a.get("title") or "").strip())
        if not label:
            continue

        is_chrome = _is_chrome_link(a)
        loc       = _link_location(a) if is_chrome else "inline"

        key = (label, absolute)
        if is_chrome:
            if key in nav_seen:
                continue
            nav_seen.add(key)
            nav_entries.append({"label": label, "url": absolute, "location": loc})
        else:
            if key in body_seen:
                continue
            body_seen.add(key)
            body_entries.append({"label": label, "url": absolute})

    nav_capped  = nav_entries[:MAX_NAV_LINKS]
    body_total  = len(body_entries)
    body_capped = body_entries[:MAX_CONTENT_LINKS]

    content_links: dict[str, Any] = {
        "total":     body_total,
        "truncated": body_total > MAX_CONTENT_LINKS,
        "items":     body_capped,
    }
    return nav_capped, content_links


def _is_chrome_link(link: Tag) -> bool:
    """True if this link lives inside a nav/header/footer/aside element."""
    CHROME_TAGS     = {"nav", "header", "footer", "aside"}
    CHROME_KEYWORDS = {"nav", "menu", "header", "footer", "sidebar", "topbar",
                       "navbar", "sidenav", "breadcrumb"}

    parent = link.parent
    while parent is not None and isinstance(parent, Tag) and parent.name != "body":
        name = (parent.name or "").lower()
        if name in CHROME_TAGS:
            return True
        combined = " ".join([
            " ".join(parent.get("class") or []),
            parent.get("id") or "",
            parent.get("role") or "",
        ]).lower()
        if any(kw in combined for kw in CHROME_KEYWORDS):
            return True
        parent = parent.parent
    return False


def _link_location(link: Tag) -> str:
    parent = link.parent
    while parent is not None and isinstance(parent, Tag):
        name = (parent.name or "").lower()
        if name == "header":
            return "header"
        if name == "footer":
            return "footer"
        if name == "aside":
            return "sidebar"
        if name == "nav":
            ancestor = parent.parent
            while ancestor is not None and isinstance(ancestor, Tag):
                aname = (ancestor.name or "").lower()
                if aname == "header":
                    return "header"
                if aname == "footer":
                    return "footer"
                if aname == "aside":
                    return "sidebar"
                ancestor = ancestor.parent
            classes = " ".join(parent.get("class", [])).lower()
            pid     = (parent.get("id") or "").lower()
            if "side" in classes or "side" in pid:
                return "sidebar"
            if "foot" in classes or "foot" in pid:
                return "footer"
            return "header"
        parent = parent.parent
    return "inline"


# ══════════════════════════════════════════════════════════════════════════════
#  Forms
# ══════════════════════════════════════════════════════════════════════════════

def _extract_forms(soup: BeautifulSoup, base_url: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    label_map = _build_label_map(soup)

    for idx, form in enumerate(soup.find_all("form")):
        action = form.get("action") or ""
        absolute_action = urljoin(base_url, action) if action else base_url
        method = (form.get("method") or "GET").upper()
        if method not in ("GET", "POST"):
            method = "GET"

        form_id = form.get("id") or f"form_{idx}"
        fields  = _extract_fields(form, label_map)
        out.append({
            "id":     form_id,
            "action": absolute_action,
            "method": method,
            "fields": fields,
        })
    return out


def _build_label_map(soup: BeautifulSoup) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for label in soup.find_all("label"):
        target = label.get("for")
        if target:
            mapping[target] = _text(label)
    return mapping


def _extract_fields(form: Tag, label_map: dict[str, str]) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []

    for el in form.find_all(["input", "select", "textarea", "button"]):
        name = el.get("name") or ""
        if el.name == "input":
            kind = (el.get("type") or "text").lower()
            if kind not in INPUT_TYPES:
                kind = "text"
        elif el.name == "select":
            kind = "select"
        elif el.name == "textarea":
            kind = "textarea"
        elif el.name == "button":
            kind = (el.get("type") or "submit").lower()
            if kind not in ("submit", "button", "reset"):
                kind = "button"
        else:
            kind = "text"

        label       = _label_for(el, label_map)
        placeholder = el.get("placeholder") or ""
        required    = el.has_attr("required")

        field: dict[str, Any] = {
            "name":        name,
            "type":        kind,
            "label":       label,
            "placeholder": placeholder,
            "required":    required,
        }

        if el.name == "select":
            field["options"] = [
                opt.get("value", _text(opt)) for opt in el.find_all("option")
            ]
            selected = el.find("option", selected=True)
            field["value"] = selected.get("value", _text(selected)) if selected else ""
        elif kind == "radio":
            radios = (
                form.find_all("input", attrs={"type": "radio", "name": name})
                if name else [el]
            )
            field["options"] = [r.get("value", "") for r in radios]
            checked = next((r for r in radios if r.has_attr("checked")), None)
            field["value"] = checked.get("value", "") if checked else ""
        elif el.name == "textarea":
            field["value"] = el.get_text() or el.get("value", "")
        elif el.name == "button":
            field["value"] = el.get("value") or _text(el)
        else:
            field["value"] = el.get("value", "")

        fields.append(field)
    return fields


def _label_for(el: Tag, label_map: dict[str, str]) -> str:
    el_id = el.get("id")
    if el_id and el_id in label_map:
        return label_map[el_id]
    aria = el.get("aria-label")
    if aria:
        return aria.strip()
    parent_label = el.find_parent("label")
    if parent_label is not None:
        text = _text(parent_label)
        if text:
            return text
    title = el.get("title")
    if title:
        return title.strip()
    return ""


# ══════════════════════════════════════════════════════════════════════════════
#  Tables
# ══════════════════════════════════════════════════════════════════════════════

def _extract_tables(soup: BeautifulSoup) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, table in enumerate(soup.find_all("table")):
        headers: list[str] = []
        thead = table.find("thead")
        if thead is not None:
            for th in thead.find_all(["th", "td"]):
                headers.append(_text(th))
        if not headers:
            first_row = table.find("tr")
            if first_row is not None and first_row.find("th") is not None:
                for th in first_row.find_all(["th", "td"]):
                    headers.append(_text(th))

        # Count candidate data rows
        body_rows = (
            table.find("tbody").find_all("tr")
            if table.find("tbody")
            else table.find_all("tr")
        )

        # Skip layout tables: no headers and many rows
        if not headers and len(body_rows) > LAYOUT_TABLE_ROWS:
            continue

        rows: list[list[str]] = []
        for row in body_rows:
            if row.find("th") is not None and not row.find("td") and not rows and headers:
                continue  # skip the header row if we already captured it
            cells = [_text(c) for c in row.find_all(["td", "th"])]
            if cells:
                rows.append(cells)
            if len(rows) >= MAX_TABLE_ROWS:
                break

        out.append({
            "id":      table.get("id") or f"table_{idx}",
            "headers": headers,
            "rows":    rows,
        })
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  Text blocks
# ══════════════════════════════════════════════════════════════════════════════

def _extract_text_blocks(soup: BeautifulSoup) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    for tag_name in ("p", "li", "blockquote"):
        for el in soup.find_all(tag_name):
            text = _text(el)
            if len(text) < MIN_TEXT_CHARS:
                continue
            if text in seen:
                continue
            seen.add(text)
            out.append({"tag": tag_name, "text": text})
            if len(out) >= MAX_TEXT_BLOCKS:
                return out
    return out


# ══════════════════════════════════════════════════════════════════════════════
#  Metadata
# ══════════════════════════════════════════════════════════════════════════════

def _extract_metadata(soup: BeautifulSoup, base_url: str) -> dict[str, Any]:
    html_tag = soup.find("html")
    lang     = (html_tag.get("lang") or "") if html_tag else ""

    canonical      = ""
    canonical_link = soup.find("link", rel="canonical")
    if canonical_link and canonical_link.get("href"):
        canonical = urljoin(base_url, canonical_link["href"])

    og_image = _meta_property(soup, "og:image")
    if og_image:
        og_image = urljoin(base_url, og_image)

    return {
        "lang":      lang,
        "canonical": canonical,
        "open_graph": {
            "title":       _meta_property(soup, "og:title"),
            "description": _meta_property(soup, "og:description"),
            "image":       og_image,
        },
    }


def _meta_content(soup: BeautifulSoup, name: str) -> str:
    tag = soup.find("meta", attrs={"name": name})
    if tag and tag.get("content"):
        return tag["content"].strip()
    return ""


def _meta_property(soup: BeautifulSoup, prop: str) -> str:
    tag = soup.find("meta", attrs={"property": prop})
    if tag and tag.get("content"):
        return tag["content"].strip()
    return ""


# ══════════════════════════════════════════════════════════════════════════════
#  Page type inference
# ══════════════════════════════════════════════════════════════════════════════

def _infer_page_type(
    soup: BeautifulSoup,
    forms: list[dict[str, Any]],
    content_links: dict[str, Any],
) -> str:
    # 1. Password field → login (strongest signal, always wins)
    for form in forms:
        for field in form["fields"]:
            if field["type"] == "password":
                return "login"

    # 2. Many repeated items → listing (check before forms so a search-bar
    #    on a results page doesn't get labelled "form")
    if _has_repeated_items(soup) or content_links["total"] >= 20:
        return "listing"

    # 3. Single content area with multiple substantive paragraphs → article
    main = soup.find("main") or soup.find("article")
    if main is not None:
        paragraphs = main.find_all("p")
        if len(paragraphs) >= 3:
            total_text = sum(len(_text(p)) for p in paragraphs)
            if total_text > 400:
                return "article"

    # 4. Multiple forms (and no listing pattern) → form
    if len(forms) >= 2:
        return "form"

    # 5. Mostly navigation links → navigation
    if _is_navigation_page(soup):
        return "navigation"

    # 6. Single form
    if len(forms) == 1:
        return "form"

    return "other"


def _has_repeated_items(soup: BeautifulSoup) -> bool:
    if len(soup.find_all("article")) >= 4:
        return True
    for ul in soup.find_all(["ul", "ol"]):
        items = ul.find_all("li", recursive=False)
        if len(items) >= 6:
            link_items = sum(1 for li in items if li.find("a") is not None)
            if link_items >= len(items) // 2:
                return True
    class_counter: Counter[str] = Counter()
    for div in soup.find_all("div"):
        classes = div.get("class") or []
        if classes:
            class_counter[" ".join(sorted(classes))] += 1
    for _, count in class_counter.most_common(3):
        if count >= 6:
            return True
    return False


_AUTH_GATED_PATTERNS = (
    "log out", "logout", "sign out", "signout", "log off",
    "my account", "my profile", "account settings", "my dashboard",
)


def _detect_auth_gated(
    soup: BeautifulSoup,
    navigation: list[dict[str, Any]],
    forms: list[dict[str, Any]],
) -> bool:
    """Return True if the page looks like a logged-in session.

    Used by the optional registry uploader so we never share private,
    user-specific content with the shared map registry. Conservative
    by design — false positives just mean a public page won't be shared,
    which costs nothing. False negatives leak private content.
    """
    for link in navigation:
        label = (link.get("label") or "").strip().lower()
        if any(p in label for p in _AUTH_GATED_PATTERNS):
            return True

    body = soup.body or soup
    for a in body.find_all("a"):
        text = _text(a).lower()
        if any(p in text for p in _AUTH_GATED_PATTERNS):
            return True

    for form in forms:
        action = (form.get("action") or "").lower()
        if "logout" in action or "signout" in action or "sign-out" in action:
            return True

    return False


def _is_navigation_page(soup: BeautifulSoup) -> bool:
    body = soup.body or soup
    links      = body.find_all("a")
    paragraphs = body.find_all("p")
    if len(links) < 5:
        return False
    if not paragraphs:
        return True
    paragraph_text = sum(len(_text(p)) for p in paragraphs)
    return len(links) > len(paragraphs) * 3 and paragraph_text < 300


# ══════════════════════════════════════════════════════════════════════════════
#  inspect_element helpers
# ══════════════════════════════════════════════════════════════════════════════

def _describe_element(el: Tag, base_url: str) -> dict[str, Any]:
    """Build a structural description of a single Tag node."""
    # Collect links within
    links = []
    for a in el.find_all("a", href=True):
        href  = (a.get("href") or "").strip()
        label = _text(a) or (a.get("aria-label") or "").strip()
        if href and label and not href.startswith(("javascript:", "mailto:", "tel:", "#")):
            links.append({"label": label, "url": urljoin(base_url, href)})

    # Collect form fields within
    fields = []
    for inp in el.find_all(["input", "select", "textarea"]):
        entry: dict[str, Any] = {
            "name":  inp.get("name") or "",
            "type":  (inp.get("type") or inp.name or "text").lower(),
            "value": inp.get("value") or "",
        }
        aria = inp.get("aria-label") or inp.get("placeholder") or inp.get("title") or ""
        if aria:
            entry["label"] = aria.strip()
        fields.append(entry)

    # Immediate children summary (tag + text, no recursion)
    children = []
    for child in el.children:
        if not isinstance(child, Tag):
            continue
        child_text = _text(child)
        child_entry: dict[str, Any] = {"tag": child.name}
        if child_text:
            child_entry["text"] = child_text[:120]
        href = child.get("href")
        if href:
            child_entry["href"] = urljoin(base_url, href)
        children.append(child_entry)

    # Build the node's own attributes (drop noisy ones)
    KEEP = {"id", "name", "type", "value", "placeholder", "href",
            "action", "method", "role", "aria-label", "for", "required"}
    attrs = {
        k: v for k, v in el.attrs.items()
        if k.lower() in KEEP and v and v != []
    }

    return {
        "tag":      el.name,
        "id":       el.get("id") or "",
        "classes":  el.get("class") or [],
        "text":     _text(el)[:300],
        "attributes": attrs,
        "links":    links[:20],
        "fields":   fields[:20],
        "children": children[:20],
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Shared text helper
# ══════════════════════════════════════════════════════════════════════════════

def _text(el: Tag | None) -> str:
    if el is None:
        return ""
    return " ".join(el.get_text(" ", strip=True).split())
