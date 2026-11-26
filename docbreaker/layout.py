"""Conservative text geometry: recover prose columns without guessing table relations."""

from dataclasses import dataclass
import math
import re
import statistics
import xml.etree.ElementTree as ET


@dataclass(frozen=True)
class Line:
    left: float
    top: float
    right: float
    bottom: float
    words: tuple

    @property
    def text(self):
        return " ".join(w[4] for w in self.words)


def read_layout(xml):
    # Poppler's external XHTML doctype is harmless; never expand injected entities.
    if b"<!ENTITY" in xml.upper():
        raise ValueError("XML entities are not supported")
    root = ET.fromstring(xml)
    page = next(n for n in root.iter() if n.tag.rsplit("}", 1)[-1] == "page")
    width, height = float(page.attrib["width"]), float(page.attrib["height"])
    if not all(math.isfinite(v) and v > 0 for v in (width, height)):
        raise ValueError("Invalid page size")
    lines = []
    for node in page.iter():
        if node.tag.rsplit("}", 1)[-1] != "line":
            continue
        words = []
        for word in node:
            if word.tag.rsplit("}", 1)[-1] == "word":
                values = tuple(float(word.attrib[k]) for k in ("xMin", "yMin", "xMax", "yMax"))
                if not all(math.isfinite(v) for v in values):
                    raise ValueError("Invalid text coordinates")
                words.append((*values, "".join(word.itertext())))
        if words:
            lines.append(Line(min(w[0] for w in words), min(w[1] for w in words),
                              max(w[2] for w in words), max(w[3] for w in words), tuple(words)))
        if len(lines) > 2000:
            raise ValueError("Dense layout")
    return width, height, lines


def prose_columns(xml, native_text):
    """Return column-ordered text only when both columns contain substantial prose.

    Require every non-whitespace source glyph to be accounted for. Short cells,
    small numeric tables, interleaved wide blocks and uncertain geometry stay on
    the visual route. All accepted lines, including headings and footers, survive.
    """
    try:
        width, _, lines = read_layout(xml)
    except (ET.ParseError, ValueError, KeyError, StopIteration, OverflowError):
        return None
    if len(lines) < 12:
        return None
    glyphs = lambda text: sorted(c for c in text if not c.isspace())
    if glyphs(native_text) != glyphs("".join(line.text for line in lines)):
        return None
    mid = width / 2
    left = [line for line in lines if line.right < mid + 5 and line.left < width * .35]
    right = [line for line in lines if line.left > mid - 5]
    if min(len(left), len(right)) < 6:
        return None
    # A stable gutter and long sentences distinguish prose from item/quantity grids.
    if min(line.left for line in right) - max(line.right for line in left) < 8:
        return None
    for column in (left, right):
        if (statistics.median(line.right - line.left for line in column) < width * .23
                or statistics.median(len(line.words) for line in column) < 5):
            return None
        for line in column:
            # Internal gaps indicate tables, formulas or diagrams inside a column.
            if any(b[0] - a[2] > max(22, (line.bottom - line.top) * 2)
                   for a, b in zip(line.words, line.words[1:])):
                return None
    top = max(min(l.top for l in left), min(l.top for l in right))
    bottom = min(max(l.bottom for l in left), max(l.bottom for l in right))
    if bottom - top < 80:
        return None
    body_top = min(l.top for l in left + right)
    body_bottom = max(l.bottom for l in left + right)
    other = [l for l in lines if l not in left and l not in right]
    if any(body_top + 10 < l.top < body_bottom - 10 for l in other):
        return None
    before = sorted((l for l in other if l.top <= body_top + 10), key=lambda l: (l.top, l.left))
    after = sorted((l for l in other if l.top > body_top + 10), key=lambda l: (l.top, l.left))
    ordered = before + sorted(left, key=lambda l: (l.top, l.left)) + sorted(right, key=lambda l: (l.top, l.left)) + after
    return "\n".join(l.text for l in ordered)


def _same_glyphs(lines, native_text):
    return sorted(c for c in native_text if not c.isspace()) == sorted(
        c for line in lines for c in line.text if not c.isspace())


def _rows(lines):
    """Group only overlapping baselines; never merge adjacent wrapped rows."""
    rows = []
    for line in sorted(lines, key=lambda item: (item.top, item.left)):
        if (not rows or abs(rows[-1][0].top - line.top) >
                min(rows[-1][0].bottom - rows[-1][0].top, line.bottom - line.top) * .4):
            rows.append([])
        rows[-1].append(line)
    return rows


def _cells(row):
    words = sorted((word for line in row for word in line.words), key=lambda word: word[0])
    cells = [[]]
    for word in words:
        if cells[-1]:
            previous = cells[-1][-1]
            if word[0] - previous[2] > max(12, (word[3] - word[1]) * 1.5):
                cells.append([])
        cells[-1].append(word)
    return cells


def _simple_table(lines):
    """Accept one rectangular, unmerged, consistently aligned table with a header.

    Keep labels, values, units and all surrounding lines. Reject charts, merged
    headers and multiline/ambiguous cells; vector evidence is checked separately.
    """
    rows = _rows(lines)
    cells = [_cells(row) for row in rows]
    indices = [i for i, row in enumerate(cells) if len(row) > 1]
    if len(indices) < 3 or indices != list(range(indices[0], indices[-1] + 1)):
        return None
    table = cells[indices[0]:indices[-1] + 1]
    count = len(table[0])
    if not 2 <= count <= 8 or any(len(row) != count for row in table):
        return None
    header = [" ".join(w[4] for w in cell) for cell in table[0]]
    if any(not any(c.isalpha() for c in label) or len(label.split()) > 8 for label in header):
        return None
    if any(not any(len("".join(w[4] for w in cell)) <= 12 for cell in row) for row in table[1:]):
        return None  # Two parallel paragraphs are not a table.
    for column in zip(*table[1:]):
        # Left-, right- or centre-aligned cells are supported, without guessing spans.
        anchors = ([cell[0][0] for cell in column], [cell[-1][2] for cell in column],
                   [(cell[0][0] + cell[-1][2]) / 2 for cell in column])
        if min(max(values) - min(values) for values in anchors) > 8:
            return None
    for a, b in zip(range(count - 1), range(1, count)):
        if max(row[a][-1][2] for row in table) + 10 > min(row[b][0][0] for row in table):
            return None
    body = [[" ".join(w[4] for w in cell).replace("|", "\\|") for cell in row] for row in table]
    markdown = ["| " + " | ".join(row) + " |" for row in body]
    markdown.insert(1, "| " + " | ".join("---" for _ in header) + " |")
    before = [line.text for row in rows[:indices[0]] for line in row]
    after = [line.text for row in rows[indices[-1] + 1:] for line in row]
    return "\n\n".join(filter(None, ("\n".join(before), "\n".join(markdown), "\n".join(after))))


def _directory(lines):
    """Numbered contents entries with aligned labels and optional descriptions."""
    rows = _rows(lines)
    if not rows or not re.search(r"目录|contents", " ".join(l.text for l in rows[0]), re.I):
        return None
    entries = []
    previous = 0
    for row in rows[1:]:
        cells = _cells(row)
        label = " ".join(w[4] for w in cells[0]).strip()
        if len(cells) == 2 and re.fullmatch(r"\d{1,2}[.、]?", label):
            number = int(label.rstrip(".、"))
            if number != previous + 1:
                return None
            previous = number
            entries.append((cells[0][0][0], cells[1][0][0], cells[1][-1][2]))
        elif len(cells) != 1 or not entries or cells[0][0][0] < entries[-1][1] - 8:
            return None
    if len(entries) < 3 or any(max(p[i] for p in entries) - min(p[i] for p in entries) > 8 for i in (0, 1)):
        return None
    return "\n".join(" ".join(l.text for l in sorted(row, key=lambda l: l.left)) for row in rows)


def _separated_columns(lines, width, height):
    """Two independent text panels with an empty gutter and no table-like rows.

    Require a heading in both panels and prose/bullets below. Numeric grids,
    wide interleaved blocks and overlapping cells stay on the visual route.
    """
    rows = _rows(lines)
    for start, row in enumerate(rows):
        if len(row) != 2 or not all(any(c.isalpha() for c in l.text) for l in row):
            continue
        pair = sorted(row, key=lambda l: l.left)
        # Derive the panel boundary from the right heading, not page midpoint.
        boundary = pair[1].left - 8
        if not width * .35 < boundary < width * .65 or pair[0].right >= boundary - 8:
            continue
        remaining = [l for r in rows[start:] for l in r]
        crossing = [l.top for l in remaining if l.left < boundary < l.right]
        footer = [l.top for l in remaining if l.top > height * .8
                  and re.match(r"\s*(?:数据来源|来源|sources?|说明)\s*[:：]", l.text, re.I)]
        end = min([height * .9, *crossing, *footer])
        body = [l for l in remaining if l.top < end - 1 and l.top < height * .9]
        left = [l for l in body if l.right < boundary]
        right = [l for l in body if l.left > boundary]
        if len(left) + len(right) != len(body) or min(len(left), len(right)) < 4:
            continue
        if any(l.top < end + 10 for l in remaining if l not in body) and end < height * .75:
            continue
        valid = True
        for column in (left, right):
            content = [l for l in column[1:] if not re.fullmatch(r"\s*\d+[.、]?\s*", l.text)]
            if (len(content) < 3 or sum(len(l.text.strip()) >= 12 for l in content) < 3
                    or any(len(_cells(r)) > 1 for r in _rows(content))):
                valid = False
                break
        if not valid:
            continue
        top = min(l.top for l in body)
        before = sorted((l for l in lines if l.top < top - 1), key=lambda l: (l.top, l.left))
        after = sorted((l for l in lines if l not in body and l not in before), key=lambda l: (l.top, l.left))
        def panel(column):
            # Attach detached numbered bullets to their baseline before ordering.
            def key(l):
                if re.fullmatch(r"\s*\d+[.、]?\s*", l.text):
                    peers = [p for p in column if p.left > l.right and abs(p.top - l.top) < max(p.bottom-p.top, l.bottom-l.top)]
                    if peers:
                        return min(p.top for p in peers), l.left
                return l.top, l.left
            return "\n".join(l.text for l in sorted(column, key=key))
        return "\n\n".join(filter(None, ("\n".join(l.text for l in before), panel(left), panel(right), "\n".join(l.text for l in after))))
    return None


def local_layout(xml, native_text):
    """Propose only complete local layouts. Caller must rule out non-text graphics."""
    try:
        width, height, lines = read_layout(xml)
    except (ET.ParseError, ValueError, KeyError, StopIteration, OverflowError):
        return None
    if not lines or not _same_glyphs(lines, native_text):
        return None
    columns = prose_columns(xml, native_text)
    if columns is not None:
        return columns, "prose_columns"
    table = _simple_table(lines)
    if table is not None:
        return table, "simple_table"
    # Headers and page numbers need not disqualify an otherwise ordinary prose page.
    before = [line for line in lines if line.bottom < height * .1]
    after = [line for line in lines if line.top > height * .9]
    body = [line for line in lines if line not in before and line not in after]
    if len(body) < 8:
        return None
    body_rows = _rows(body)
    if (all(len(_cells(row)) == 1 for row in body_rows)
            and statistics.median(line.right - line.left for line in body) > width * .5
            and statistics.median(len(line.text) for line in body) > 35):
        # Detached punctuation can have a slightly different baseline. Assemble
        # the proven single-cell row left-to-right instead of interleaving lines.
        blocks = [" ".join(word[4] for word in _cells(row)[0]) for row in body_rows]
        return "\n".join([*(l.text for l in sorted(before, key=lambda l: (l.top, l.left))),
                          *blocks, *(l.text for l in sorted(after, key=lambda l: (l.top, l.left)))]), "prose_with_margins"
    # A footer containing a source label and page number is not a second table.
    table = _simple_table(body)
    if table is not None:
        return "\n\n".join(filter(None, (
            "\n".join(l.text for l in sorted(before, key=lambda l: (l.top, l.left))),
            table, "\n".join(l.text for l in sorted(after, key=lambda l: (l.top, l.left)))
        ))), "simple_table"
    directory = _directory(lines)
    if directory is not None:
        return directory, "directory"
    columns = _separated_columns(lines, width, height)
    if columns is not None:
        return columns, "separated_columns"
    # Reuse the strict column proof on the body, retaining every margin glyph outside it.
    root = ET.Element("html")
    page = ET.SubElement(root, "page", width=str(width), height=str(height))
    for line in body:
        element = ET.SubElement(page, "line")
        for word in line.words:
            item = ET.SubElement(element, "word", **dict(zip(("xMin", "yMin", "xMax", "yMax"), map(str, word[:4]))))
            item.text = word[4]
    columns = prose_columns(ET.tostring(root), "\n".join(line.text for line in body))
    if columns is not None:
        return "\n".join([*(line.text for line in sorted(before, key=lambda l: (l.top, l.left))),
                          columns, *(line.text for line in sorted(after, key=lambda l: (l.top, l.left)))]), "prose_columns_with_margins"
    return None


def text_and_rules_only(svg, xml=None, kind=None):
    """Allow text, rules and proven decorations; keep body graphics on the model route.

    Cairo outlines glyphs in defs; those are text, while visible paths, shapes and
    non-glyph uses need explicit bounds. Only small margin logos and proven table
    header backgrounds are exempt. Fail closed on unsupported SVG structures.
    """
    if b"<!ENTITY" in svg.upper():
        return False
    try:
        root = ET.fromstring(svg)
        if root.tag.rsplit("}", 1)[-1] != "svg":
            return False
        width, height, lines = read_layout(xml) if xml is not None else (0, 0, [])
        references = {node.get("id"): node for node in root.iter() if node.get("id")}
        margin_image_area = 0.
        number = r"[-+\d.eE]+"
        rectangle = re.compile(rf"\s*M\s*({number})[ ,]+({number})\s*L\s*({number})[ ,]+({number})\s*L\s*({number})[ ,]+({number})\s*L\s*({number})[ ,]+({number})\s*Z(?:\s*M\s*({number})[ ,]+({number}))?\s*")
        def box(x, y, w, h, matrix):
            sx, sy, tx, ty = matrix
            xs, ys = (x*sx+tx, (x+w)*sx+tx), (y*sy+ty, (y+h)*sy+ty)
            return min(xs), min(ys), max(xs), max(ys)

        def background_or_header(bounds, fill):
            fill = re.sub(r"\s+", "", fill).lower()
            x1, y1, x2, y2 = bounds
            if not all(math.isfinite(v) for v in bounds) or x2 <= x1 or y2 <= y1:
                return False
            if (fill in {"white", "#fff", "#ffffff", "rgb(100%,100%,100%)", "rgb(255,255,255)"}
                    and x1 <= 0 and y1 <= 0 and x2 >= width > 0 and y2 >= height > 0):
                return True
            if kind != "simple_table":
                return False
            # Only filled cells of the proven table's header, not bars or legends.
            for row in _rows(lines):
                cells = _cells(row)
                if len(cells) < 2 or not all(any(c.isalpha() for c in "".join(w[4] for w in cell)) for cell in cells):
                    continue
                if y1 <= row[0].top and y2 >= max(l.bottom for l in row):
                    contained = [c for c in cells if x1 <= c[0][0] and x2 >= c[-1][2]]
                    return (len(contained) == 1 and y2-y1 <= max(l.bottom-l.top for l in row)*3.5
                            and x1 >= 0 and x2 <= width)
                return False
            return False

        def safe(node, parent=(1., 1., 0., 0.)):
            nonlocal margin_image_area
            tag = node.tag.rsplit("}", 1)[-1]
            if tag in {"defs", "clipPath"}:
                return True
            style = dict((k.strip(), v.strip()) for part in node.attrib.get("style", "").split(";")
                         if ":" in part for k,v in [part.split(":",1)])
            attrs = {**style, **node.attrib}
            current = parent
            transform = node.attrib.get("transform", "")
            if transform:
                matrix = re.fullmatch(r"matrix\(([-+\d.eE,\s]+)\)", transform)
                if not matrix:
                    return False
                values = [float(v) for v in re.split(r"[,\s]+", matrix[1].strip())]
                if len(values) != 6 or not all(math.isfinite(v) for v in values) or values[1] or values[2]:
                    return False  # A rotated/skewed rule may be a diagram edge.
                sx, sy, tx, ty = parent
                a, _, _, d, e, f = values
                current = (sx*a, sy*d, sx*e+tx, sy*f+ty)
            if tag == "use":
                href = node.attrib.get("{http://www.w3.org/1999/xlink}href", node.attrib.get("href", ""))
                if href.startswith("#glyph"):
                    return True
                image = references.get(href.lstrip("#"))
                if not lines or image is None or image.tag.rsplit("}", 1)[-1] != "image":
                    return False
                bounds = box(*(float(image.get(k, "0")) for k in ("x", "y", "width", "height")), current)
                x1, y1, x2, y2 = bounds
                if (not all(math.isfinite(v) for v in bounds) or not (0 <= x1 < x2 <= width)
                        or not (0 <= y1 < y2 <= height) or not (y2 <= height*.08 or y1 >= height*.92)):
                    return False
                margin_image_area += (x2-x1)*(y2-y1)
                return margin_image_area <= width*height*.015
            if tag == "rect" and lines:
                bounds = box(*(float(node.get(k, "0")) for k in ("x", "y", "width", "height")), current)
                return background_or_header(bounds, attrs.get("fill", ""))
            if tag == "path":
                if attrs.get("fill") != "none":
                    match = rectangle.fullmatch(attrs.get("d", ""))
                    if not match or not lines:
                        return False
                    values = list(map(float, match.groups()[:8]))
                    points = list(zip(values[::2], values[1::2]))
                    xs, ys = {p[0] for p in points}, {p[1] for p in points}
                    if len(xs) != 2 or len(ys) != 2 or any(a[0] != b[0] and a[1] != b[1] for a,b in zip(points, points[1:]+points[:1])):
                        return False
                    return background_or_header(box(min(xs), min(ys), max(xs)-min(xs), max(ys)-min(ys), current), attrs.get("fill", ""))
                if not 0 < float(attrs.get("stroke-width", 1)) <= 2:
                    return False
                values = re.fullmatch(r"\s*M\s*([-+\d.eE]+)[ ,]+([-+\d.eE]+)\s*L\s*([-+\d.eE]+)[ ,]+([-+\d.eE]+)\s*", attrs.get("d", ""))
                if not values:
                    return False
                x1, y1, x2, y2 = map(float, values.groups())
                return all(math.isfinite(v) for v in (x1, y1, x2, y2)) and (abs(x2-x1) < .1 or abs(y2-y1) < .1)
            if tag not in {"svg", "g"}:
                return False
            return all(safe(child, current) for child in node)

        return safe(root)
    except (ET.ParseError, ValueError, KeyError, StopIteration, OverflowError):
        return False


def restore_native_headings(xml, text):
    """Mark only isolated, visibly larger native lines as headings.

    Use relative glyph height, not a vocabulary of benchmark titles. Repeated
    body-sized labels and table cells stay unchanged. Never reorder source text.
    """
    try:
        _, _, lines = read_layout(xml)
    except (ET.ParseError, ValueError, KeyError, StopIteration, OverflowError):
        return text
    body = [l.bottom - l.top for l in lines if len(l.text.strip()) >= 40]
    if len(body) < 4:
        return text
    height = statistics.median(body)
    candidates = [l for l in lines if l.bottom - l.top >= height * 1.25
                  and 2 <= len(l.text.strip()) <= 160
                  and any(c.isalpha() for c in l.text)
                  and not l.text.rstrip().endswith(('.', '。', ';', '；'))]
    sizes = sorted({round((l.bottom - l.top) / height, 1) for l in candidates}, reverse=True)
    result = text
    for line in candidates:
        # A same-baseline neighbor indicates table cells, not a heading.
        if any(other is not line and abs(other.top - line.top) < height * .4
               for other in lines):
            continue
        pattern = r'(?m)^[ \t]*' + r'[ \t]+'.join(re.escape(w[4]) for w in line.words) + r'[ \t]*$'
        # Require a unique full source line. Avoid repeated labels and code.
        if len(re.findall(pattern, result)) != 1:
            continue
        level = min(6, sizes.index(round((line.bottom - line.top) / height, 1)) + 1)
        result = re.sub(pattern, lambda m: '#' * level + ' ' + m[0].strip(), result)
    return result
