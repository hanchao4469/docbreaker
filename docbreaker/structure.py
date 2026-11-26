"""Document-wide native typography hints; never infer content from filenames."""
from collections import Counter, defaultdict
from dataclasses import dataclass
import math
import re
import xml.etree.ElementTree as ET


def canonical(text):
    return re.sub(r'\s+', '', text).replace('‑', '-').replace('−', '-')


def _number(node, name):
    value = float(node.attrib[name])
    if not math.isfinite(value):
        raise ValueError('Non-finite geometry')
    return value


@dataclass(frozen=True)
class TextLine:
    text: str
    size: float
    bold: bool
    code: bool
    top: float
    bottom: float
    left: float
    right: float
    split: bool


class DocumentStructure:
    def __init__(self):
        self.pages = {}
        self.headings = {}
        self.furniture = set()

    @classmethod
    def from_xml(cls, xml):
        if b'<!ENTITY' in xml.upper():
            raise ValueError('XML entities are not supported')
        root = ET.fromstring(xml)
        result = cls()
        fonts = {}
        body = Counter()
        for page in root.findall('page'):
            number = int(page.attrib['number'])
            height = _number(page, 'height')
            if height <= 0:
                raise ValueError('Invalid page height')
            for f in page.findall('fontspec'):
                fonts[f.attrib['id']] = (_number(f, 'size'), f.attrib.get('family', ''))
            spans = []
            for t in page.findall('text'):
                text = ''.join(t.itertext()).strip()
                if not text:
                    continue
                size, family = fonts[t.attrib['font']]
                top, left = _number(t, 'top'), _number(t, 'left')
                bottom, right = top + _number(t, 'height'), left + _number(t, 'width')
                bold_text = ''.join(''.join(x.itertext()) for x in t.findall('.//b'))
                bold = len(canonical(bold_text)) >= len(canonical(text)) * .8
                code = bool(re.search('mono|courier|consolas|sourcecode|menlo|inconsolata', family, re.I))
                spans.append((top, left, bottom, right, text, size, bold, code))
            groups = []
            for s in sorted(spans, key=lambda s: (s[0], s[1])):
                # Baselines, rather than tops, align syntax spans and line numbers.
                group = next((g for g in reversed(groups[-3:]) if abs(g[0][2] - s[2]) <= max(2, s[5] * .2)), None)
                if group is None:
                    groups.append([s])
                else:
                    group.append(s)
            lines = []
            for group in groups:
                group.sort(key=lambda s: s[1])
                code_spans = [s for s in group if s[7]]
                code = bool(code_spans) and sum(len(s[4]) for s in code_spans) >= sum(len(s[4]) for s in group) * .7
                if code:
                    # Drop a renderer's code-gutter line index only, not code digits.
                    group = [s for s in group if s[7] or not (s[4].isdigit() and s[1] < code_spans[0][1])]
                parts = []
                for i, s in enumerate(group):
                    if i and s[1] - group[i-1][3] > max(1.5, s[5] * .15):
                        parts.append(' ')
                    parts.append(s[4])
                text = ''.join(parts)
                size = max(s[5] for s in group)
                split = any(b[1] - a[3] > size * 2 for a,b in zip(group,group[1:]))
                bold = sum(len(s[4]) for s in group if s[6]) >= len(canonical(text)) * .8
                line = TextLine(text, size, bold, code, min(s[0] for s in group), max(s[2] for s in group), group[0][1], max(s[3] for s in group), split)
                lines.append(line)
                if not code and len(text) >= 50 and not bold:
                    body[size] += len(text)
            result.pages[number] = (height, sorted(lines, key=lambda l:(l.top,l.left)))
        if not body:
            return result
        body_size = body.most_common(1)[0][0]
        candidates = []
        margins = defaultdict(set)
        for number, (height, lines) in result.pages.items():
            for l in lines:
                if l.top < height * .05 or l.bottom > height * .95:
                    if len(l.text) <= 100:
                        margins[canonical(l.text)].add(number)
                if (not l.code and not l.split and 2 <= len(l.text) <= 160
                    and any(c.isalpha() for c in l.text)
                    and not l.text.rstrip().endswith(('.', '。', ';', '；', ',', '，'))
                    and (l.size >= body_size * 1.12 or l.bold and l.size >= body_size * .98)):
                    candidates.append((number,l))
        # Small documents do not provide enough repetition evidence to drop text.
        result.furniture = {text for text,pages in margins.items() if len(pages) >= max(3, math.ceil(len(result.pages)*.6))}
        sizes = sorted({l.size for _,l in candidates if canonical(l.text) not in result.furniture}, reverse=True)
        for number,l in candidates:
            if canonical(l.text) not in result.furniture:
                result.headings.setdefault(number, {})[canonical(l.text)] = min(6, sizes.index(l.size)+1)
        return result

    def context(self, number):
        headings = self.headings.get(number, {})
        _, lines = self.pages.get(number, (0, []))
        return [{'text': l.text, 'level': headings[canonical(l.text)]} for l in lines if canonical(l.text) in headings][:40]

    def format_page(self, number, text):
        """Only mark uniquely matching native source lines; never rewrite prose."""
        headings = self.headings.get(number, {})
        _, source_lines = self.pages.get(number, (0, []))
        output = text.splitlines()
        counts = Counter(canonical(re.sub(r'^#{1,6}\s+', '', s.strip())) for s in output if s.strip())
        code_keys = {canonical(l.text):l.text for l in source_lines if l.code and not l.split}
        fence_marker = None
        code_run = []
        result = []
        def flush():
            if code_run:
                n = max([2]+[len(m[0]) for l in code_run for m in re.finditer(r'`+',l)])+1
                fence='`'*n
                result.extend([fence,*code_run,fence]);code_run.clear()
        for line in output:
            marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
            if marker and (fence_marker is None or (marker[1][0] == fence_marker[0] and len(marker[1]) >= len(fence_marker) and not marker[2].strip())):
                flush()
                fence_marker = marker[1] if fence_marker is None else None
                result.append(line)
                continue
            if fence_marker is not None:
                result.append(line);continue
            if line.lstrip().startswith(("|", "<")):
                flush();result.append(line);continue
            clean = re.sub(r'^#{1,6}\s+', '', line.strip())
            key = canonical(clean)
            # Source code gutters are visual furniture; require exact code glyphs.
            code_key = key if key in code_keys else canonical(re.sub(r'^\d{1,5}\s+', '', clean))
            if code_key in code_keys and counts[key] == 1:
                code_run.append(re.sub(r'^\d{1,5}\s+', '', line) if key not in code_keys else line);continue
            flush()
            if key in self.furniture:
                continue
            if key in headings and counts[key] == 1:
                result.append('#'*headings[key]+' '+clean)
            else:
                result.append(line)
        flush()
        return '\n'.join(result)
