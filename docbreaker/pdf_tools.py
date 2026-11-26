"""Bounded, cancellable Poppler processes and conservative page routing."""

import asyncio
import math
import os
import re
import signal
import time
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .events import ParseError, event
from .layout import local_layout, read_layout, text_and_rules_only, restore_native_headings


@dataclass
class Page:
    number: int
    text: str
    reasons: list[str]
    width: float
    height: float
    native_markdown: str | None = None
    text_regions: tuple = ()


def route_reasons(text: str, xml: bytes, image_area: float) -> list[str]:
    """Use glyphs, image coverage and word geometry; short text alone is not failure."""
    reasons = []
    if not text.strip():
        reasons.append("no_native_text")
    if any(unicodedata.category(c) in {"Co", "Cs"} or c == "\ufffd"
           or (unicodedata.category(c) == "Cc" and c not in "\n\r\t\f") for c in text):
        reasons.append("invalid_glyphs")
    if re.search(r"\(cid:\d+\)|[\u0378\u0379]", text):
        reasons.append("unmapped_glyphs")
    # Outlined/vector glyphs can be invisible to pdftotext and pdfimages alike.
    # A page with only a year/page number/bullets is not evidence of complete text.
    letters = sum(unicodedata.category(c).startswith("L") for c in text)
    if text.strip() and letters < 8 and sum(c.isalnum() for c in text) < 40:
        reasons.append("sparse_native_text")
    if image_area > 0.015:
        reasons.append("image_content")
    try:
        if b"<!ENTITY" in xml.upper():
            raise ValueError("entities")
        root = ET.fromstring(xml)
        lines = []
        for line in root.iter():
            if line.tag.rsplit("}", 1)[-1] != "line":
                continue
            words = [w for w in line if w.tag.rsplit("}", 1)[-1] == "word"]
            if words:
                coords = [(float(w.attrib["xMin"]), float(w.attrib["yMin"]),
                           float(w.attrib["xMax"]), float(w.attrib["yMax"])) for w in words]
                if not all(math.isfinite(v) for p in coords for v in p):
                    raise ValueError("nonfinite")
                lines.append((min(p[0] for p in coords), min(p[1] for p in coords),
                              max(p[2] for p in coords), max(p[3] for p in coords), coords))
                if len(lines) > 2000:
                    return list(dict.fromkeys([*reasons, "dense_layout_uncertain"]))
        grid_rows = 0
        for left, top, right, bottom, words in lines:
            height = max(1, bottom - top)
            if sum(b[0] - a[2] > max(20, height * 1.8) for a, b in zip(words, words[1:])) >= 1:
                grid_rows += 1
        if grid_rows >= 2:
            reasons.append("table_or_columns")
        # Separate text blocks on the same baseline are common in tables and diagrams.
        ordered = sorted(lines, key=lambda line: line[1])
        for i, a in enumerate(ordered):
            threshold = max(3, (a[3] - a[1]) * .4)
            for b in ordered[i + 1:]:
                if b[1] - a[1] >= threshold:
                    break
                if b[0] - a[2] > 20 or a[0] - b[2] > 20:
                    reasons.append("disjoint_text_blocks")
                    break
            if "disjoint_text_blocks" in reasons:
                break
        # Very sparse pages can contain vector art absent from pdfimages output.
        if 0 < len(lines) <= 2 and len(text.strip()) < 12:
            reasons.append("sparse_layout_uncertain")
    except (ET.ParseError, ValueError, KeyError, OverflowError):
        reasons.append("layout_uncertain")
    return list(dict.fromkeys(reasons))


class PdfTools:
    def __init__(self, settings: Settings, task_id: str, render_limiter=None, native_limiter=None):
        self.settings = settings
        self.task_id = task_id
        self.render_limiter = render_limiter
        self.native_limiter = native_limiter

    async def run(self, tool: str, args: list[str], cwd: Path, page: int | None = None) -> bytes:
        """Never invoke a shell; kill the whole process group on timeout/cancellation."""
        started = time.monotonic()
        # Concurrent pages must never truncate or unlink another subprocess's output.
        descriptor, name = tempfile.mkstemp(prefix=f".{tool}.", suffix=".out", dir=cwd)
        os.close(descriptor)
        output = Path(name)
        process = None
        try:
            with output.open("wb") as stream:
                process = await asyncio.create_subprocess_exec(
                    tool, *args, stdout=stream, stderr=asyncio.subprocess.DEVNULL,
                    env={**os.environ, "LC_ALL": "C"}, start_new_session=True,
                )
                try:
                    await asyncio.wait_for(process.wait(), self.settings.command_timeout)
                except BaseException:
                    if process.returncode is None:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        await process.wait()
                    raise
            if process.returncode:
                raise ParseError("invalid_pdf", "Poppler could not read this PDF; check encryption or file integrity")
            if output.stat().st_size > 16 * 1024**2:
                raise ParseError("page_too_large", "PDF tool output exceeded the page limit")
            return output.read_bytes()
        except TimeoutError:
            raise ParseError("tool_timeout", "PDF processing exceeded the tool time limit") from None
        finally:
            output.unlink(missing_ok=True)
            event("stage_finished", task_id=self.task_id, page=page, stage=tool,
                  duration_ms=round((time.monotonic() - started) * 1000))

    async def document_structure(self, source: Path):
        """Optional typography pass; unavailable metadata must not fail parsing."""
        from .structure import DocumentStructure
        async def extract():
            xml = await self.run("pdftohtml", ["-xml", "-i", "-stdout", str(source)], source.parent)
            return DocumentStructure.from_xml(xml)
        try:
            if self.native_limiter:
                async with self.native_limiter:
                    return await extract()
            return await extract()
        except (ParseError, OSError, ValueError, KeyError, ET.ParseError):
            event("structure_hints_unavailable", task_id=self.task_id)
            return DocumentStructure()

    async def inspect(self, source: Path) -> int:
        if self.native_limiter:
            async with self.native_limiter:
                return await self._inspect(source)
        return await self._inspect(source)

    async def _inspect(self, source: Path) -> int:
        info = (await self.run("pdfinfo", [str(source)], source.parent)).decode("utf-8", "replace")
        if re.search(r"^Encrypted:\s+yes", info, re.M):
            raise ParseError("encrypted_pdf", "Encrypted PDFs are not supported")
        match = re.search(r"^Pages:\s+(\d+)", info, re.M)
        if not match or not 1 <= int(match[1]) <= self.settings.max_pages:
            raise ParseError("page_limit", "PDF page count exceeds the configured limit")
        return int(match[1])

    async def extract(self, source: Path, number: int, check_figures: bool = True) -> Page:
        if self.native_limiter:
            async with self.native_limiter:
                return await self._extract(source, number, check_figures)
        return await self._extract(source, number, check_figures)

    async def _extract(self, source: Path, number: int, check_figures: bool) -> Page:
        args = ["-f", str(number), "-l", str(number), "-enc", "UTF-8"]
        try:
            text = (await self.run("pdftotext", [*args, "-layout", str(source), "-"], source.parent, number)).decode("utf-8", "replace").strip("\f\n")
            xml = await self.run("pdftotext", [*args, "-bbox-layout", str(source), "-"], source.parent, number)
        except ParseError as exc:
            if exc.code != "invalid_pdf":
                raise
            # A valid PDF can have broken text/font metadata and still render correctly.
            text, xml = "", b""
            event("native_extraction_fallback", task_id=self.task_id, page=number, code=exc.code)
        width, height = 595., 842.
        fallback = False
        text_regions = ()
        try:
            width, height, native_lines = read_layout(xml)
            text_regions = tuple({"text": line.text, "bounds": (line.left/width, line.top/height, line.right/width, line.bottom/height)} for line in native_lines)
        except (ET.ParseError, StopIteration, KeyError, ValueError, OverflowError):
            fallback = True
            info = (await self.run("pdfinfo", ["-f", str(number), "-l", str(number),
                                               "-box", str(source)], source.parent, number)).decode("utf-8", "replace")
            size = re.search(r"(?:Page\s+\d+\s+size|Page size):\s+([\d.]+)\s+x\s+([\d.]+)", info)
            if size:
                dimensions = tuple(float(v) for v in size.groups())
                if all(math.isfinite(v) and v > 0 for v in dimensions):
                    width, height = dimensions
            # XML metadata is advisory. Rendering + the model can recover the page.
            event("layout_metadata_fallback", task_id=self.task_id, page=number)
        images = (await self.run("pdfimages", ["-f", str(number), "-l", str(number), "-list", str(source)], source.parent, number)).decode()
        area = 0.
        for line in images.splitlines():
            parts = line.split()
            if len(parts) >= 14 and parts[0].isdigit() and parts[2] == "image":
                try:
                    xppi, yppi = float(parts[12]), float(parts[13])
                    if min(xppi, yppi) > 0:
                        area += (int(parts[3]) * 72 / xppi) * (int(parts[4]) * 72 / yppi) / (width * height)
                    else:
                        area = 1.
                except (ValueError, ZeroDivisionError):
                    area = 1.
        reasons = route_reasons(text, xml, area)
        native_markdown = None
        # Relax geometry warnings only with complete native glyph coverage and
        # independent vector inspection. A paragraph-heavy page may still contain a chart.
        geometry = {"disjoint_text_blocks", "table_or_columns", "sparse_layout_uncertain"}
        if reasons and set(reasons) <= geometry | {"image_content", "visual_content"} and not fallback:
            candidate = local_layout(xml, text)
            if candidate is not None:
                # A table-shaped group of chart labels is not a proven table until
                # graphic inspection passes. Keep ambiguous tables on full vision.
                if candidate[1] != "simple_table":
                    native_markdown = restore_native_headings(xml, candidate[0])
                try:
                    svg = await self.run("pdftocairo", ["-svg", "-f", str(number), "-l", str(number),
                                                       str(source), "-"], source.parent, number)
                except ParseError as exc:
                    svg = b""
                    event("local_layout_rejected", task_id=self.task_id, page=number, code=exc.code)
                if text_and_rules_only(svg, xml, candidate[1]):
                    text, kind = candidate
                    reasons = []
                    event("local_layout_recovered", task_id=self.task_id, page=number, layout=kind)
        elif check_figures and not reasons:
            # Visual details can be embedded as vector paths even on otherwise simple text pages.
            try:
                svg = await self.run("pdftocairo", ["-svg", "-f", str(number), "-l", str(number),
                                                   str(source), "-"], source.parent, number)
            except ParseError:
                svg = b""
            if not text_and_rules_only(svg, xml):
                reasons.append("visual_content")
                # No geometry/glyph warning: the text layer has a usable order.
                native_markdown = restore_native_headings(xml, text)
        if fallback and "layout_uncertain" not in reasons:
            reasons.append("layout_uncertain")
        if not reasons:
            native_markdown = None
            text = restore_native_headings(xml, text)
        return Page(number, text, reasons, width, height, native_markdown, text_regions)

    async def render(self, source: Path, page: int, scale: int = 1) -> Path:
        """Render only the requested page with a strict maximum image dimension."""
        if self.render_limiter:
            async with self.render_limiter:
                return await self._render(source, page, scale)
        return await self._render(source, page, scale)

    async def _render(self, source: Path, page: int, scale: int) -> Path:
        prefix = source.parent / f"page-{page}"
        await self.run("pdftoppm", ["-f", str(page), "-l", str(page), "-singlefile", "-jpeg",
                                   "-jpegopt", "quality=90", "-r", str(self.settings.render_dpi * scale),
                                   "-scale-to", str(self.settings.render_max_pixels * scale),
                                   str(source), str(prefix)], source.parent, page)
        result = prefix.with_suffix(".jpg")
        if not result.exists() or result.stat().st_size > 15 * 1024**2:
            raise ParseError("render_limit", "Rendered page exceeds the image limit")
        return result
