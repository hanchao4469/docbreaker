"""Deterministic extraction of visible content and product facts from public HTML."""
import json
import re
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

from bs4 import BeautifulSoup, Comment
from markdownify import MarkdownConverter

from .web_fetch import normalize

NOISE = (
    "nav, footer, [role=navigation], [role=banner], [role=contentinfo], [role=dialog], "
    "form, button, input, select, textarea, style, script, noscript, svg, canvas, "
    "dialog, [aria-modal=true], [inert], .visually-hidden, .sr-only, "
    ".shopify-section-group-header-group, .shopify-section-group-footer-group, "
    ".announcement-bar, .cookie-banner, .newsletter-popup, .quick-add, "
    ".breadcrumbs-wrap, .breadcrumb, .m-popup, .mc_prize-popup, .popup-view, "
    "sticky-add-to-cart, product-recommendations, .product-sticky-bar, "
    "product-recommendations, recently-viewed-products, .related-products, .product-recommendations, "
    "[id^=shopify-block-][class*=app-block]"
)
WIKI = 'page > template[slot="contents"]'
PRODUCT = re.compile(r"/products/[^/]+/?$", re.I)
SHOPIFY_META = re.compile(r"(?:ShopifyAnalytics\.meta\s*=\s*|var meta\s*=\s*)(?=\{)")
AMOUNT_FIELDS = ("price", "lowPrice", "highPrice", "priceCurrency", "availability", "itemCondition")
# Facts in accessibility metadata may outlive a replaced banner or product image.
# Retain the label for image selection, but do not index it as visible page text.
FACTUAL_IMAGE_ALT = re.compile(
    r"(?<![0-9])(?:19|20)[0-9]{2}(?![0-9])"
    r"|[0-9][0-9,.]*\s*(?:名|人|個|件|円|元|%|％|people\b|persons?\b|winners?\b|units?\b)"
    r"|(?:[$€£¥￥]\s*[0-9])", re.I)



class SafeMarkdown(MarkdownConverter):
    def escape(self, text, parent_tags):
        return super().escape(text, parent_tags).replace("<", "&lt;").replace(">", "&gt;")


def plain(value):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    return " ".join(BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True).split()) if "<" in str(value) else " ".join(str(value).split())


def label(value):
    return plain(value).replace("|", "&#124;").replace("<", "&lt;").replace(">", "&gt;")


def find_types(value, kinds):
    if isinstance(value, list):
        for item in value:
            yield from find_types(item, kinds)
    if isinstance(value, dict):
        types = value.get("@type", [])
        types = types if isinstance(types, list) else [types]
        if any(isinstance(t, str) and t.rsplit("/", 1)[-1] in kinds for t in types):
            yield value
        for key in ("@graph", "mainEntity", "hasVariant"):
            yield from find_types(value.get(key), kinds)


def facts(soup, page_url):
    product_facts = []
    descriptions = []
    currency = ""
    by_sku = {}
    for node in soup.select('script[type="application/ld+json"]')[:40]:
        try:
            data = json.loads(node.string or node.get_text())
            for product in find_types(data, {"Product", "ProductGroup"}):
                fields = [("Product", product.get("name")), ("SKU", product.get("sku")),
                          ("Model", product.get("model")), ("Brand", product.get("brand"))]
                description = plain(product.get("description"))
                if description and description not in descriptions:
                    descriptions.append(description)
                for item in product.get("additionalProperty") or []:
                    if isinstance(item, dict):
                        fields.append((plain(item.get("name")), item.get("value")))
                offers = product.get("offers") or []
                offers = offers if isinstance(offers, list) else [offers]
                if len(offers) <= 1:
                    for offer in offers:
                        if isinstance(offer, dict):
                            currency = plain(offer.get("priceCurrency")) or currency
                            if sku := plain(product.get("sku")):
                                by_sku[sku] = offer
                            else:
                                fields.extend((k, offer.get(k)) for k in AMOUNT_FIELDS)
                else:
                    for offer in offers:
                        if isinstance(offer, dict):
                            currency = plain(offer.get("priceCurrency")) or currency
                            variant = plain(offer.get("name") or offer.get("sku") or offer.get("url"))
                            fields.append(("Offer", " · ".join(filter(None, [variant, *(plain(offer.get(k)) for k in AMOUNT_FIELDS)]))))
                for key, value in fields:
                    if isinstance(value, dict):
                        value = value.get("name")
                    if key and (clean := label(value)):
                        if key in {"availability", "itemCondition"}:
                            clean = clean.rsplit("/", 1)[-1]
                        entry = f"- **{label(key)}:** {clean}"
                        if entry not in product_facts:
                            product_facts.append(entry)
        except (ValueError, TypeError, RecursionError):
            continue
    page_product_id = None
    analytics_variants = []
    explicit_variants = []
    if PRODUCT.search(urlsplit(page_url).path):
        for script in soup.find_all("script"):
            raw = script.string or ""
            try:
                if match := SHOPIFY_META.search(raw):
                    payload, _ = json.JSONDecoder().raw_decode(raw[match.end():])
                    product = payload.get("product") or {}
                    if product.get("variants"):
                        page_product_id = product.get("id")
                        analytics_variants = product["variants"]
                if script.get("type") == "application/json" and raw.lstrip().startswith("{") and '"variants"' in raw:
                    payload = json.loads(raw)
                    if isinstance(payload.get("variants"), list) and payload.get("id"):
                        explicit_variants.append(payload)
            except (ValueError, TypeError, AttributeError, RecursionError):
                continue
        selected = next((p for p in explicit_variants if page_product_id and
                         str(p["id"]) == str(page_product_id)), None)
        if not selected:
            handle = urlsplit(page_url).path.rstrip("/").rsplit("/", 1)[-1]
            selected = next((p for p in explicit_variants if p.get("handle") == handle), None)
        variants = selected["variants"] if selected else analytics_variants
        # The analytics object lists price/SKU but often lacks availability. Use the
        # current product's variant selector JSON and SKU-specific schema offers.
        theme = {}
        for script in soup.select('main variant-picker script[type="application/json"], '
                                  'main variant-selects script[type="application/json"]'):
            try:
                value = json.loads(script.string or "")
            except (ValueError, RecursionError):
                continue
            for item in value if isinstance(value, list) else [value]:
                if isinstance(item, dict) and item.get("id") and type(item.get("available")) is bool:
                    theme[str(item["id"])] = item
        if theme and not variants:
            variants = list(theme.values())
        if variants:
            table = ["| Variant | SKU | Price | Availability |", "| --- | --- | --- | --- |"]
            for v in variants:
                if not isinstance(v, dict):
                    continue
                v = {**v, **theme.get(str(v.get("id")), {})}
                offer = by_sku.get(plain(v.get("sku")), {})
                name = label(v.get("public_title") or v.get("name") or v.get("title") or v.get("id"))
                price = v.get("price")
                if offer.get("price") is not None:
                    price = " ".join(filter(None, [label(offer["price"]), label(offer.get("priceCurrency"))]))
                else:
                    price = f"{price/100:.2f} {currency}".strip() if type(price) is int else label(price)
                availability = plain(offer.get("availability")).rsplit("/", 1)[-1]
                available = availability or {True: "Available to order", False: "Out of stock"}.get(
                    v.get("available") if type(v.get("available")) is bool else None, "Not provided")
                table.append(f"| {name} | {label(v.get('sku'))} | {price} | {available} |")
            if len(table) > 2:
                product_facts.extend(["", "### Variants", "", *table])
        if not variants:
            for sku, offer in by_sku.items():
                details = " · ".join(filter(None, [label(sku), *(label(offer.get(k)) for k in AMOUNT_FIELDS)]))
                product_facts.append(f"- **Offer:** {details}")
    return product_facts, descriptions


def schema_faq(soup):
    for node in soup.select('script[type="application/ld+json"]')[:40]:
        try:
            data = json.loads(node.string or "")
            for question in find_types(data, {"Question"}):
                name = plain(question.get("name"))
                answer = question.get("acceptedAnswer")
                if name and isinstance(answer, dict) and (text := plain(answer.get("text"))):
                    yield name, text
        except (ValueError, TypeError, RecursionError):
            continue


def extract(html, url, *, discover_links=True, locate_images=False):
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one("form#challenge-form, #cf-challenge-running, #shopify-challenge, form[action='/password']"):
        return {"error": "challenge_page", "links": []}
    links = list(dict.fromkeys(v for a in soup.find_all("a", href=True) if (v := normalize(a["href"], url)))) if discover_links else []
    canonical = soup.select_one('link[rel=canonical][href]')
    canonical = normalize(canonical["href"], url) if canonical else None
    source = soup.select_one(WIKI)
    if source:
        root = BeautifulSoup(source.decode_contents(), "html.parser")
        if discover_links:
            links.extend(v for a in root.find_all("a", href=True) if (v := normalize(a["href"], url)))
    else:
        root = soup.select_one("main, [role=main]") or soup.find("article") or soup.body
    if root is None:
        return {"error": "empty_body", "links": links}
    product = bool(PRODUCT.search(urlsplit(url).path) or soup.select_one('meta[property="og:type"][content=product]'))
    product_facts, descriptions = facts(soup, url) if product else ([], [])
    faq = list(dict.fromkeys(schema_faq(soup)))
    # Document identity comes from the browser tab, not a logo or section H1.
    title = plain(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if not title:
        shared_title = soup.select_one('meta[property="og:title"][content]')
        title = plain(shared_title.get("content")) if shared_title else ""
    for node in root.select(NOISE):
        if node.parent is not None:
            node.decompose()
    for node in root.find_all(string=lambda s: isinstance(s, Comment)):
        node.extract()
    if not title:
        for heading in root.find_all("h1"):
            if not heading.find_parent(["header", "nav", "footer"]):
                title = plain(heading.get_text(" ", strip=True))
                if title:
                    break
    # Some pages emit malformed closing breaks, causing html.parser to put
    # subsequent text inside a void tag. Markdown conversion drops its children.
    for br in root.find_all("br"):
        if br.contents:
            br.insert_before(soup.new_tag("br"))
            br.unwrap()
    resources = {}
    seen_image_text = set()
    image_markers = {}
    marker_prefix = f"DOCBREAKERIMAGE{uuid4().hex}X" if locate_images else ""
    for h in root.find_all("h1"):
        if h.get_text(" ", strip=True) == title:
            h.decompose()
    for node in root.find_all(["template", "iframe", "video", "source", "img", "a"]):
        if node.parent is None:
            continue
        # Preserve linked media; replace embeds with safe links and drop layout-only images.
        if node.name == "template":
            node.decompose()
            continue
        attr = "href" if node.name == "a" else "src"
        value = (node.get("data-src") or node.get(attr)) if node.name == "img" else node.get(attr)
        if not value and node.name == "video":
            child = node.find("source", src=True)
            value = child.get("src") if child else None
        href = urljoin(url, value) if value else ""
        if not href.startswith(("https://", "http://")) or re.search(r"[\x00-\x20<>]", href):
            node.unwrap() if node.name == "a" else node.decompose()
            continue
        if node.name == "img":
            alt = " ".join((node.get("alt") or "").split())
            # Keep descriptive source text, but avoid repeating responsive variants
            # and short presentation labels such as "Map" and "Point 1".
            descriptive = len(alt) >= 24
            replacement = ""
            factual = bool(FACTUAL_IMAGE_ALT.search(alt))
            if descriptive and not factual and alt not in seen_image_text:
                seen_image_text.add(alt)
                replacement = f" {alt} "
            if descriptive and not factual and not replacement and not node.find_parent("a"):
                node.decompose()
                continue
            if not re.fullmatch(r"logo|icon|badge|banner|image|placeholder", alt, re.I):
                resources.setdefault(href, {"url": href, "label": alt, "type": "image"})
                if marker_prefix and (image_url := normalize(href, url)):
                    marker = f"{marker_prefix}{len(image_markers)}END"
                    table = node.find_parent("table")
                    raw_html = bool(table and table.select_one("[rowspan], [colspan]"))
                    image_markers[marker] = (image_url, raw_html)
                    # Avoid creating a nested link when the image itself is clickable.
                    if anchor := node.find_parent("a"):
                        anchor.insert_after(marker)
                        if replacement:
                            node.replace_with(replacement)
                        else:
                            node.decompose()
                    else:
                        node.replace_with(replacement + marker)
                    continue
            if replacement:
                node.replace_with(replacement)
            else:
                node.decompose()
        elif node.name == "a":
            node.attrs = {"href": href}
            if urlsplit(href).path.lower().endswith((".pdf", ".zip", ".docx", ".xlsx")):
                resources.setdefault(href, {"url": href, "label": node.get_text(" ", strip=True), "type": "attachment"})
        else:
            anchor = soup.new_tag("a", href=href)
            anchor.string = node.get("title") or "Video"
            resources.setdefault(href, {"url": href, "label": str(anchor.string), "type": "video"})
            node.replace_with(anchor)
    # Preserve merged cell structure through a sanitized table-only HTML fragment.
    complex_tables = {}
    for index, table in enumerate(root.select("table:has([rowspan], [colspan])")):
        if table.parent is None:
            continue
        cleaned = BeautifulSoup(str(table), "html.parser")
        tags = {"table", "caption", "thead", "tbody", "tfoot", "tr", "th", "td", "p", "strong", "em", "ul", "ol", "li", "a", "br"}
        for tag in cleaned.find_all(True):
            if tag.name not in tags:
                tag.unwrap()
            else:
                tag.attrs = {k: v for k, v in tag.attrs.items() if
                             (k in {"rowspan", "colspan"} and re.fullmatch(r"[1-9]\d{0,2}", str(v)))
                             or (tag.name == "a" and k == "href")}
        marker = f"DOCBREAKERTABLEMARKER{index}END"
        complex_tables[marker] = str(cleaned)
        paragraph = soup.new_tag("p")
        paragraph.string = marker
        table.replace_with(paragraph)
    # Main headings and accordion bodies remain even when the website initially collapses them.
    md = SafeMarkdown(heading_style="ATX", bullets="-", strip=["summary"]).convert_soup(root).strip()
    for marker, table in complex_tables.items():
        md = md.replace(marker, table)
    md = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", md)
    md = re.sub(r"(?m)^\s*(?:add to cart|buy it now|quick view|subscribe|learn more|view all|shop now|choose options)\s*$", "", md, flags=re.I)
    md = re.sub(r"(?im)^.*(?:spin\s*(?:&|and)\s*win|enter your email to subscribe).*$",
                lambda m: "" if not re.search(r"warranty|shipping|return", m[0], re.I) else
                re.sub(r"(?i)\s*\+\s*spin\s*(?:&|and)\s*win.*$", "", m[0]), md)
    marker_pattern = re.compile(marker_prefix + r"\d+END") if image_markers else None
    visible = " ".join(" ".join(root.stripped_strings).split())
    if marker_pattern:
        visible = marker_pattern.sub("", visible)
    # Theme markup can split words around punctuation or formatting tags.
    def comparable(value):
        return re.sub(r"[\W_]+", "", value.casefold())
    comparable_visible = comparable(visible)
    supplemental_faq = [(q, a) for q, a in faq if comparable(a) not in comparable_visible]
    if supplemental_faq:
        md += "\n\n## Additional FAQ\n\n" + "\n\n".join(f"### {label(q)}\n\n{label(a)}" for q, a in supplemental_faq)
    if descriptions:
        extra = [d for d in descriptions if d not in visible]
        if extra:
            product_facts.extend(["", "### Product description", "", *(label(d) for d in extra)])
    thin_content = (
        len(re.sub(r"!\[[^\]]*\]\([^\n]*?\)|[\W_]", "", marker_pattern.sub("", md) if marker_pattern else md)) < 55
        and not product_facts
    )
    if title:
        md = f"# {label(title)}\n\n{md}"
    if product_facts:
        md += "\n\n## Product data\n\n" + "\n".join(product_facts)
    image_positions = []
    if marker_pattern:
        removed = 0
        for match in marker_pattern.finditer(md):
            if match[0] in image_markers:
                image_url, raw_html = image_markers[match[0]]
                image_positions.append((match.start() - removed, image_url, raw_html))
            removed += len(match[0])
        md = marker_pattern.sub("", md)
    if thin_content and not image_positions:
        return {"error": "thin_content", "links": links}
    return {"title": title, "markdown": md, "canonical": canonical,
            **({"_requires_image_text": True} if thin_content else {}),
            "type": "product" if product else "page", "links": list(dict.fromkeys(links)),
            "resources": list(resources.values()),
            **({"_image_positions": image_positions} if image_positions else {})}
