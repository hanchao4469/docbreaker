"""Normalize transport wrappers and text markup without inventing page content."""

import json
import re
from html.parser import HTMLParser
from html import escape

from .events import ParseError


PAGE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "markdown": {"type": "string"},
        "uncertain": {"type": "boolean"},
        "figures": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                **{key: {"type": "integer"} for key in ("left", "top", "right", "bottom")},
                "steps": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["left", "top", "right", "bottom", "steps"],
        }},
        "uncertainty_reasons": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["markdown", "uncertain", "uncertainty_reasons", "figures"],
}
RESPONSE_FORMAT = {"type": "json_schema", "json_schema": {
    "name": "pdf_page_extraction", "strict": True, "schema": PAGE_SCHEMA,
}}


TEXT_RESPONSE_FORMAT = {"type": "json_schema", "json_schema": {
    **RESPONSE_FORMAT["json_schema"], "schema": {
        **PAGE_SCHEMA,
        "properties": {k: v for k, v in PAGE_SCHEMA["properties"].items() if k != "figures"},
        "required": [k for k in PAGE_SCHEMA["required"] if k != "figures"],
    },
}}


class TextMarkup(HTMLParser):
    """Render common generated HTML as inert Markdown; never fetch any asset."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.tables = []
        self.changed = False

    def append(self, value):
        if self.tables and self.tables[-1]["cell"] is not None:
            self.tables[-1]["cell"].append(value)
        else:
            self.parts.append(value)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.changed = True
        if tag == "img":
            self.append(attrs.get("alt", ""))
        elif tag == "table":
            self.tables.append({"rows": [], "row": [], "cell": None, "merged": False})
        elif tag == "tr" and self.tables:
            self.handle_endtag("td")
            self.handle_endtag("tr")
        elif tag in {"td", "th"} and self.tables:
            self.handle_endtag("td")
            table = self.tables[-1]
            table["cell"] = []
            table["merged"] |= attrs.get("rowspan", "1") != "1" or attrs.get("colspan", "1") != "1"
        elif tag == "br":
            self.append("\n")
        elif tag in {"p", "div", "section", "article", "ul", "ol", "pre"}:
            self.append("\n\n")
        elif re.fullmatch(r"h[1-6]", tag):
            self.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.append("\n- ")
        elif tag in {"b", "strong"}:
            self.append("**")
        elif tag in {"i", "em"}:
            self.append("*")
        elif tag == "code":
            self.append("`")
        elif tag not in {"a", "span", "thead", "tbody", "tfoot", "caption"}:
            # Literal tags in source code/labels remain text, not active HTML.
            self.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.tables:
            table = self.tables[-1]
            if table["cell"] is not None:
                table["row"].append("".join(table["cell"]).strip())
                table["cell"] = None
        elif tag == "tr" and self.tables:
            self.handle_endtag("td")
            table = self.tables[-1]
            if table["row"]:
                table["rows"].append(table["row"])
                table["row"] = []
        elif tag == "table" and self.tables:
            table = self.tables.pop()
            if table["cell"] is not None:
                table["row"].append("".join(table["cell"]).strip())
            if table["row"]:
                table["rows"].append(table["row"])
            rows = table["rows"]
            if table["merged"]:
                # Keep merged rows as rows; do not guess a rectangular association.
                value = "\n".join("- " + " | ".join(row) for row in rows)
            elif rows:
                width = max(map(len, rows))
                lines = ["| " + " | ".join(cell.replace("|", r"\|").replace("\n", " ")
                         for cell in row + [""] * (width - len(row))) + " |" for row in rows]
                lines.insert(1, "| " + " | ".join(["---"] * width) + " |")
                value = "\n".join(lines)
            else:
                value = ""
            self.append("\n\n" + value + "\n\n")
        elif tag in {"b", "strong"}:
            self.append("**")
        elif tag in {"i", "em"}:
            self.append("*")
        elif tag == "code":
            self.append("`")
        elif tag in {"p", "div", "section", "article", "pre"} or re.fullmatch(r"h[1-6]", tag):
            self.append("\n\n")
        elif tag not in {"a", "span", "ul", "ol", "li", "thead", "tbody", "tfoot", "caption", "img", "br"}:
            self.append(f"</{tag}>")

    def handle_data(self, data):
        self.append(data)

    def handle_comment(self, data):
        self.changed = True
        self.append("<!--" + data + "-->")

    def text(self):
        # Do not silently drop text from an unclosed table.
        while self.tables:
            self.handle_endtag("table")
        return "".join(self.parts).replace("<", "&lt;")


class SafeTable(HTMLParser):
    """Keep table topology without retaining active HTML or resource attributes."""

    tags = {"table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption", "br"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.tags:
            return
        spans = ""
        if tag in {"td", "th"}:
            for name, value in attrs:
                if name in {"rowspan", "colspan"} and value and re.fullmatch(r"[1-9][0-9]{0,2}", value):
                    spans += f' {name}="{value}"'
        self.parts.append(f"<{tag}{spans}>")

    def handle_endtag(self, tag):
        if tag in self.tags and tag != "br":
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(escape(data, quote=False))


def preserve_structures(markdown):
    """Protect code/math and span tables before the legacy prose normalizer.

    Tokens use a source-absent prefix so literal document content cannot collide.
    Simple tables retain the existing Markdown conversion behavior.
    """
    prefix = "DOCBREAKERPROTECTED"
    while prefix in markdown:
        prefix += "X"
    protected = []

    def keep(value):
        token = f"{prefix}{len(protected)}END"
        # Math delimiters are not an HTML sandbox in every Markdown consumer.
        # Retain comparisons, but never restore active/resource tags from math.
        if value.startswith("$"):
            value = re.sub(r"<(?=/?(?:script|style|iframe|img|svg|math|object|embed|link|video|audio|source|form|input|button)\b)",
                           "&lt;", value, flags=re.I)
        protected.append((token, value))
        return token

    # Do not interpret HTML-looking source code or LaTeX as document markup.
    pattern = r"(`{3,}[^\n]*\n[\s\S]*?\n`{3,}|`[^`\n]+`|\$\$[\s\S]*?\$\$|(?<!\\)\$(?!\$)[^\n$]+(?<!\\)\$)"
    markdown = re.sub(pattern, lambda m: keep(m[0]), markdown)

    def table(match):
        value = match[0]
        if not re.search(r"\b(?:rowspan|colspan)\s*=", value, re.I):
            return value
        parser = SafeTable()
        parser.feed(value)
        parser.close()
        return keep("".join(parser.parts))

    # In Markdown table rows a cell break must stay inline, never split the row.
    markdown = re.sub(r"(?m)^\s*\|.*\|\s*$",
                      lambda m: re.sub(r"<br\s*/?>", lambda _: keep("<br>"), m[0], flags=re.I), markdown)
    markdown = re.sub(r"<table\b[^>]*>[\s\S]*?</table\s*>", table, markdown, flags=re.I)
    return markdown, protected


def normalize_markdown(markdown):
    repairs = []
    markdown, protected = preserve_structures(markdown)
    # Drop only the image embed itself, retaining its textual label/caption.
    markdown, count = re.subn(r"!\[([^\]\n]*)\]\s*(?:\((?:[^()\n]|\([^()\n]*\))*\)|\[[^\]\n]*\])",
                               lambda m: m[1], markdown)
    if count:
        repairs.append("image_labels_preserved")
    parser = TextMarkup()
    parser.feed(markdown)
    parser.close()
    markdown = parser.text()
    if parser.changed:
        repairs.append("html_converted_to_text")
    # Escape incomplete embeds or literal data URIs as text rather than assets.
    if "![" in markdown or re.search("data:image", markdown, re.I):
        markdown = markdown.replace("![", "!\\[")
        markdown = re.sub("data:image", "data&#58;image", markdown, flags=re.I)
        repairs.append("asset_syntax_escaped")
    for token, value in reversed(protected):
        markdown = markdown.replace(token, value)
    return markdown.strip(), repairs


def parse_page_output(raw, *, allow_empty=False):
    """Accept valid text with harmless wrapper deviations; never salvage truncated JSON."""
    repairs = []
    if isinstance(raw, list) and raw and all(isinstance(p, dict) and p.get("type") == "text"
                                           and isinstance(p.get("text"), str) for p in raw):
        raw = "\n".join(p["text"] for p in raw)
        repairs.append("content_blocks_joined")
    if not isinstance(raw, str):
        raise ParseError("invalid_model_output", "Model did not return page text")
    raw = raw.strip().lstrip("\ufeff")
    fence = re.fullmatch(r"```(?:json|markdown|md)?\s*\n?(.*?)\n?```", raw, re.S | re.I)
    if fence:
        raw = fence[1].strip()
        repairs.append("outer_fence_removed")
    try:
        parsed = json.loads(raw)
    except ValueError:
        # Some gateways return a complete object with a short preamble. Decode one
        # complete object only; do not repair quotes, escapes or missing braces.
        start = raw.find("{")
        try:
            if start < 0:
                raise ValueError()
            parsed, _ = json.JSONDecoder().raw_decode(raw[start:])
            if not isinstance(parsed, dict) or "markdown" not in parsed:
                raise ValueError()
            repairs.append("json_wrapper_removed")
        except ValueError:
            # Plain Markdown from gateways ignoring response_format is usable text.
            # JSON-looking invalid responses must be regenerated, never published.
            if not raw or raw.startswith(("{", "[", '"')) or re.search(r'"markdown"\s*:', raw):
                raise ParseError("invalid_model_output", "Model returned malformed page JSON") from None
            parsed = {"markdown": raw, "uncertain": True}
            repairs.append("plain_markdown")
    if not isinstance(parsed, dict) or not isinstance(parsed.get("markdown"), str):
        raise ParseError("invalid_model_output", "Model response is missing page Markdown")
    uncertain = parsed.get("uncertain", True)
    if not isinstance(uncertain, bool):
        if isinstance(uncertain, str) and uncertain.lower() in {"true", "false"}:
            uncertain = uncertain.lower() == "true"
        else:
            uncertain = True
        repairs.append("uncertainty_normalized")
    elif "uncertain" not in parsed:
        repairs.append("uncertainty_defaulted")
    markdown, markup_repairs = normalize_markdown(parsed["markdown"])
    if not markdown and not allow_empty:
        raise ParseError("empty_model_output", "Model omitted a non-blank page")
    result = {"markdown": markdown, "uncertain": uncertain, "output_repairs": repairs + markup_repairs}
    result["uncertainty_reasons"] = [v[:256] for v in parsed.get("uncertainty_reasons", [])[:8]
                                     if isinstance(v, str)] if isinstance(parsed.get("uncertainty_reasons"), list) else []
    if isinstance(parsed.get("figures"), list):
        result["figures"] = parsed["figures"][:3]
    return result
