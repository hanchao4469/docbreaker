"""Scope, discovery and quality accounting for a bounded crawl."""
import asyncio
from collections import Counter, deque
import fnmatch
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from .events import event
from .web_extract import extract
from .web_fetch import BOT, WebError, normalize, private_path, same_site

ASSET = re.compile(r"\.(?:png|jpe?g|gif|svg|webp|avif|pdf|zip|css|js|mp4|webm|xml|json|woff2?|ico)(?:$|/)", re.I)
KINDS = ("product", "support", "article", "collection", "other")
TRANSIENT = {"network_error", "http_429", "http_500", "http_502", "http_503", "http_504"}


def kind(url):
    path = urlsplit(url).path.lower()
    if "/products/" in path:
        return "product"
    if "/policies/" in path or any(x in path for x in ("faq", "support", "warranty", "return", "refund", "shipping", "manual", "contact", "help-center")):
        return "support"
    if "/blogs/" in path:
        return "article"
    if "/collections/" in path:
        return "collection"
    return "other"


class Robots:
    def __init__(self, body):
        groups = []
        agents, rules = [], []
        self.sitemaps = []
        for raw in body.decode("utf-8-sig", errors="replace").splitlines():
            field, _, value = raw.split("#", 1)[0].partition(":")
            field, value = field.strip().lower(), value.strip()
            if not value:
                continue
            if field == "sitemap":
                self.sitemaps.append(value)
            elif field == "user-agent":
                if rules:
                    groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(value.lower())
            elif field in ("allow", "disallow") and agents and len(value) <= 2048:
                end = value.endswith("$")
                # Robots only treats * and terminal $ as special. fnmatch compiles
                # consecutive wildcards without pathological regex backtracking.
                expr = "".join({"?": "[?]", "[": "[[]"}.get(c, c) for c in (value[:-1] if end else value))
                pattern = fnmatch.translate(expr + ("" if end else "*"))
                rules.append((len(value.replace("*", "").removesuffix("$")), field == "allow",
                              re.compile(pattern)))
        groups.append((agents, rules))
        exact = [r for a, r in groups if BOT.lower() in a]
        self.rules = [rule for r in (exact or [r for a, r in groups if "*" in a]) for rule in r]

    def allows(self, url):
        p = urlsplit(url)
        path = p.path + ("?" + p.query if p.query else "")
        return max(((size, allow) for size, allow, pattern in self.rules if pattern.match(path)),
                   default=(0, True))[1]


def sitemap_links(body):
    # Do not parse entity declarations from untrusted XML.
    if b"\x00" in body or re.search(rb"<!\s*(?:DOCTYPE|ENTITY)", body, re.I):
        raise WebError("invalid_sitemap")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise WebError("invalid_sitemap") from exc
    tag = root.tag.rsplit("}", 1)[-1]
    if tag not in {"urlset", "sitemapindex"}:
        raise WebError("invalid_sitemap")
    return tag == "sitemapindex", [x.text.strip() for x in root.findall("./{*}*/{*}loc") if x.text]


async def crawl(fetcher, seed, max_pages, progress=lambda _: None, *, full_site, enrich=None):
    rules = {}
    inventory = {}
    entries = {k: deque() for k in KINDS}
    origin = seed
    pages, attempts, discovery = [], [], []
    visited, canonicals, hashes = set(), set(), set()
    counters = Counter()
    bytes_out = 0
    inventory_limited = False
    deadline = asyncio.get_running_loop().time() + 510

    async def permitted(url):
        p = urlsplit(url)
        key = (p.scheme, p.netloc)
        if key not in rules:
            try:
                _, body, mime = await fetcher.get(f"{p.scheme}://{p.netloc}/robots.txt", seed, limit=512_000)
                if mime == "text/html" or body.lstrip().lower().startswith((b"<!doctype html", b"<html")):
                    raise WebError("robots_not_rules")
                rules[key] = Robots(body)
            except WebError as exc:
                if exc.code in {"http_404", "http_410"}:
                    rules[key] = Robots(b"")
                elif not full_site and exc.code in {"redirect_out_of_scope", "robots_not_rules"}:
                    # A manually selected public page is not recursive crawling.
                    # Do not follow maintenance redirects or mistake HTML for rules.
                    event("web_robots_invalid_single_page", host=p.hostname, reason=exc.code)
                    rules[key] = Robots(b"")
                else:
                    raise WebError("robots_unavailable") from exc
        return rules[key].allows(url)

    def discover(raw, via, base):
        nonlocal inventory_limited
        url = normalize(raw, base)
        if not url or not same_site(url, origin) or (origin.startswith("https:") and not url.startswith("https:")):
            return
        p = urlsplit(url)
        if private_path(url) or ASSET.search(p.path):
            return
        # The product page carries structured variants; pagination and other content parameters stay distinct.
        if kind(url) == "product" and re.fullmatch(r"variant=\d+", p.query):
            url = url.split("?")[0]
        if url not in inventory:
            if len(inventory) >= 20_000:
                inventory_limited = True
                return
            inventory[url] = via
            entries[kind(url)].append(url)

    async def fetch_page(url):
        for attempt in range(2):
            try:
                final, body, mime = await fetcher.get(url, origin, permitted=permitted)
                if mime not in ("text/html", "application/xhtml+xml", ""):
                    raise WebError("unsupported_content")
                try:
                    parsed = await asyncio.to_thread(extract, body, final, discover_links=full_site,
                                                     locate_images=enrich is not None)
                except (ValueError, TypeError, AttributeError, RecursionError):
                    parsed = {"error": "extraction_failed", "links": []}
                return final, parsed
            except WebError as exc:
                if attempt or exc.code not in TRANSIENT or exc.code == "http_429":
                    return url, {"error": exc.code, "links": []}
                await asyncio.sleep(1)

    async def record(url, final, parsed):
        nonlocal bytes_out
        visited.update((url, final))
        links = parsed.pop("links")
        image_positions = parsed.pop("_image_positions", [])
        if full_site:
            for link in links:
                discover(link, final, final)
        error = parsed.get("error")
        requires_image_text = parsed.pop("_requires_image_text", False)
        if requires_image_text:
            # Image-only pages can still be useful, but only with visible text
            # recovered from the image. Never fall back to a stale alt claim.
            markdown = parsed["markdown"]
            if enrich and asyncio.get_running_loop().time() + 30 < deadline:
                parsed["markdown"] = await enrich(final, parsed, image_positions, permitted)
            if parsed["markdown"] == markdown:
                error = "thin_content"
        canonical = parsed.get("canonical")
        # Canonicals often point all collection pages at page 1; preserve distinct content parameters.
        canonical_key = (canonical if canonical and same_site(canonical, origin)
                         and urlsplit(canonical).query == urlsplit(final).query else final)
        if not error:
            data = parsed["markdown"].encode()
            digest = hashlib.sha256(data).hexdigest()
            page_size = len(json.dumps(parsed, ensure_ascii=False).encode())
            if digest in hashes or (canonical_key, digest) in canonicals:
                error = "duplicate_content"
            elif page_size > 350_000 or bytes_out + page_size > 10_000_000:
                error = "output_limit"
            else:
                hashes.add(digest)
                canonicals.add((canonical_key, digest))
                if enrich and not requires_image_text and asyncio.get_running_loop().time() + 30 < deadline:
                    markdown = await enrich(final, parsed, image_positions, permitted)
                    if markdown != parsed["markdown"]:
                        updated = dict(parsed, markdown=markdown)
                        size = len(json.dumps(updated, ensure_ascii=False).encode())
                        if size <= 350_000 and bytes_out + size <= 10_000_000:
                            parsed, page_size = updated, size
                bytes_out += page_size
                pages.append(dict(parsed, url=final, sha256=hashlib.sha256(parsed["markdown"].encode()).hexdigest()))
        counters[error or "extracted"] += 1
        attempts.append({"url": url, "final_url": final, "discovered_from": inventory[url], "status": error or "extracted"})
        progress(len(attempts))

    discover(seed, "seed", seed)
    if seed not in inventory:
        raise WebError("unsupported_seed")
    final, parsed = await fetch_page(seed)
    if same_site(final, seed):
        origin = final
    await record(seed, final, parsed)
    if not full_site or any(counters[k] for k in ("robots_unavailable", "non_public_address", "http_429")):
        return {"pages": pages, "attempts": attempts, "counts": dict(counters), "discovery": discovery,
                "discovered": len(inventory), "pending": 0}
    p = urlsplit(origin)
    base = f"{p.scheme}://{p.netloc}"
    home = base + "/"
    if home not in visited:
        discover(home, "site_home", origin)
        if home in entries["other"]:
            entries["other"].remove(home)
            entries["other"].appendleft(home)
    maps = deque(dict.fromkeys([*(rules.get((p.scheme, p.netloc), Robots(b"")).sitemaps), base + "/sitemap.xml"]))
    seen_maps = set()
    while maps and len(seen_maps) < 16 and asyncio.get_running_loop().time() < deadline:
        sitemap = normalize(maps.popleft(), origin)
        if not sitemap or not same_site(sitemap, origin) or sitemap in seen_maps:
            continue
        seen_maps.add(sitemap)
        try:
            final_map, body, _ = await fetcher.get(sitemap, origin, limit=3_000_000, permitted=permitted)
            nested, locs = sitemap_links(body)
            discovery.append({"url": final_map, "entries": len(locs), "type": "index" if nested else "sitemap"})
            if nested:
                maps.extend(locs[:100])
            else:
                for loc in locs:
                    discover(loc, final_map, final_map)
        except WebError as exc:
            discovery.append({"url": sitemap, "error": exc.code})
            if exc.code == "http_429":
                return {"pages": pages, "attempts": attempts, "counts": dict(counters), "discovery": discovery,
                        "discovered": len(inventory), "pending": len(inventory.keys() - visited),
                        "pending_sitemaps": len(maps), "rate_limited": True}

    while len(attempts) < max_pages and any(entries.values()):
        for group in KINDS:
            while entries[group] and entries[group][0] in visited:
                entries[group].popleft()
            if asyncio.get_running_loop().time() >= deadline:
                return {"pages": pages, "attempts": attempts, "counts": dict(counters), "discovery": discovery,
                        "discovered": len(inventory), "pending": len(inventory.keys() - visited),
                        "pending_sitemaps": len(maps), "time_limit": True}
            if entries[group] and len(attempts) < max_pages:
                url = entries[group].popleft()
                final, parsed = await fetch_page(url)
                await record(url, final, parsed)
                if parsed.get("error") == "http_429":
                    return {"pages": pages, "attempts": attempts, "counts": dict(counters), "discovery": discovery,
                            "discovered": len(inventory), "pending": len(inventory.keys() - visited), "rate_limited": True}
    return {"pages": pages, "attempts": attempts, "counts": dict(counters), "discovery": discovery,
            "discovered": len(inventory), "pending": len(inventory.keys() - visited), "pending_sitemaps": len(maps), "inventory_limited": inventory_limited}
