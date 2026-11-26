"""Per-page extraction with explicit native-text degradation and ordered assembly."""

import re
import asyncio
import hashlib
import json
import time
from pathlib import Path

from PIL import Image

from .config import Settings
from .events import ParseError, event
from .model_client import ModelClient, native_text_coverage
from .pdf_tools import PdfTools
from .pdf_figures import MAX_FIGURES, save_figures


def local_markdown(text: str) -> str:
    """Preserve all characters and source order; strip only layout padding."""
    paragraphs = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line.strip() for line in block.splitlines()]
        paragraphs.append("\n".join(lines))
    # Literal source strings must not become image/HTML assets in downstream Markdown.
    return "\n\n".join(paragraphs).replace("![", "!\\[").replace("<", "&lt;")


async def parse_pdf(source: Path, filename: str, task_id: str, settings: Settings,
                    model: ModelClient, progress) -> dict:
    """Bound concurrent pages globally and report any degraded/missing page content."""
    tools = PdfTools(settings, task_id, model.render_limiter, model.native_limiter)
    cache_version = hashlib.sha256(("quality-v12-source-text-and-verified-diagrams|" + settings.model + model.system_prompt
                                    + str(settings.render_max_pixels)).encode()).hexdigest()
    progress({"stage": "inspect"})
    page_count = await tools.inspect(source)
    structure = await tools.document_structure(source)
    progress({"page_count": page_count})
    finished = {}
    progress({"failed_pages": []})
    numbers = iter(range(1, page_count + 1))

    async def read_page(number):
        started = time.monotonic()
        checkpoint = source.parent / f"checkpoint-{number:05}.json"
        try:
            cached = json.loads(checkpoint.read_text())
        except (OSError, ValueError):
            cached = {}
        if (isinstance(cached, dict) and cached.get("version") == cache_version
                and isinstance(cached.get("info"), dict) and cached["info"].get("page") == number
                and isinstance(cached.get("markdown"), str) and isinstance(cached.get("native"), str)
                and not cached["info"].get("missing_content")
                and not cached["info"].get("fallback_error")):
            finished[number] = cached["info"]
            event("page_resumed", task_id=task_id, page=number)
        else:
            progress({"stage": "extract", "current_page": number})
            # Visual text inspection is required even when image export is disabled.
            page = await tools.extract(source, number)
            # Labels/page numbers do not prove that surrounding raster paragraphs were read.
            # Use supplement-only extraction only when the native layer contains actual prose.
            if page.native_markdown is not None and not re.search(
                    r"[.!?。！？]|[\u3400-\u9fff]{20}", page.native_markdown) and len(page.native_markdown.split()) < 20:
                page.native_markdown = None
            route = ("hybrid" if page.native_markdown is not None else "llm") if page.reasons else "local"
            event("page_routed", task_id=task_id, page=number, parser=route, reasons=page.reasons,
                  native_characters=len(page.text))
            progress({"stage": route, "route_reasons": page.reasons, "current_page": number})
            uncertain = False
            fallback_error = None
            output_repairs = []
            uncertainty_reasons = []
            figures = []
            if route in {"llm", "hybrid"}:
                image = await tools.render(source, number)
                try:
                    blank = False
                    if not page.text.strip():
                        with Image.open(image) as rendered:
                            with rendered.convert("L") as grayscale:
                                blank = grayscale.getextrema()[0] >= 250
                    if blank:
                        route, markdown = "blank", ""
                    else:
                        if route == "hybrid":
                            # Persist independently of a visual call, including when that call fails.
                            (source.parent / f"native-{number:05}.md").write_text(local_markdown(page.native_markdown))
                            result = await model.read_page(task_id, number, image, page.native_markdown,
                                                           filename, visual_only=True)
                        else:
                            result = await model.read_page(task_id, number, image, page.text, filename)
                        output_repairs.extend(result.get("output_repairs", []))
                        if result["uncertain"]:
                            event("page_closer_look", task_id=task_id, page=number)
                            image = await tools.render(source, number, scale=2)
                            try:
                                reviewed = await model.read_page(task_id, number, image, page.native_markdown or page.text,
                                                               filename, closer_look=True,
                                                               **({"visual_only": True} if route == "hybrid" else {}))
                                # A closer read must not replace fuller source text with a shorter transcription.
                                first_coverage = native_text_coverage(page.text, result["markdown"])
                                review_coverage = native_text_coverage(page.text, reviewed["markdown"])
                                if review_coverage >= first_coverage and (not result["markdown"] or
                                        len(reviewed["markdown"]) >= len(result["markdown"]) * .85):
                                    result = reviewed
                                else:
                                    output_repairs.append("fuller_initial_transcription_retained")
                                output_repairs.extend(reviewed.get("output_repairs", []))
                            except ParseError as exc:
                                if exc.code not in {"invalid_model_output", "empty_model_output", "model_incomplete",
                                                    "model_network_error", "model_unavailable", "model_image_limit"}:
                                    raise
                                # The first extraction is complete but flagged uncertain.
                                # A failed verification must not erase that usable result.
                                output_repairs.append("closer_look_failed")
                                event("page_closer_look_failed", task_id=task_id, page=number, code=exc.code)
                        markdown, uncertain = result["markdown"], result["uncertain"]
                        uncertainty_reasons = result.get("uncertainty_reasons", [])
                        if route == "hybrid":
                            native = local_markdown(page.native_markdown)
                            markdown = native + ("\n\n" + markdown if markdown else "")
                            output_repairs.append("native_text_preserved")
                        if model.figures and result.get("figures"):
                            draft_figures = result["figures"]
                            try:
                                checked = await model.read_page(task_id, number, image, page.text, filename,
                                                                visual_only=True, figure_draft=result["figures"])
                                if checked["uncertain"] or not checked.get("figures"):
                                    result["figures"] = [{**f, "steps": ["AI diagram interpretation could not be verified; refer to the original image."]}
                                                         for f in draft_figures if isinstance(f, dict)]
                                    uncertain = True
                                    uncertainty_reasons.append("figure_interpretation_unverified")
                                else:
                                    result["figures"] = checked["figures"]
                                uncertain = uncertain or checked["uncertain"]
                                uncertainty_reasons.extend(checked.get("uncertainty_reasons", []))
                                output_repairs.append("figure_interpretation_withheld" if checked["uncertain"] or not checked.get("figures")
                                                      else "figure_interpretation_verified")
                            except ParseError as exc:
                                if exc.code not in {"invalid_model_output", "empty_model_output", "model_incomplete",
                                                    "model_network_error", "model_unavailable", "model_image_limit", "model_refused"}:
                                    raise
                                # Keep the source image; do not publish an unverified instruction.
                                result["figures"] = [{**f, "steps": ["AI diagram interpretation unavailable; refer to the original image."]}
                                                     for f in result["figures"] if isinstance(f, dict)]
                                uncertain = True
                                uncertainty_reasons.append("figure_verification_failed")
                                output_repairs.append("figure_interpretation_withheld")
                        if model.figures and result.get("figures"):
                            # No await here: shared budget is reserved within the event loop.
                            for orphan in source.parent.glob(f"figure-{number}-*.jpg"):
                                orphan.unlink(missing_ok=True)
                            used = sum(1 for _ in source.parent.glob("figure-*.jpg"))
                            try:
                                figures = save_figures(image, result["figures"], number, MAX_FIGURES - used, page.text_regions)
                            except (OSError, ValueError):
                                event("figure_crop_failed", task_id=task_id, page=number)
                        if not markdown and not figures:
                            raise ParseError("empty_model_output", "Model omitted a non-blank page")
                except ParseError as exc:
                    if exc.code not in {"invalid_model_output", "empty_model_output", "model_incomplete",
                                        "model_network_error", "model_unavailable", "model_image_limit", "model_refused"}:
                        raise
                    # A model failure affects this page, not the whole document.
                    # Preserve only actual native text, never malformed model output.
                    fallback_error = exc.code
                    markdown = local_markdown(page.native_markdown if page.native_markdown is not None else page.text)
                    route, uncertain, figures = "local_fallback", True, []
                    event("page_native_fallback", task_id=task_id, page=number, code=exc.code,
                          native_characters=len(markdown), missing_content=not any(c.isalpha() for c in markdown),
                          missing_visual_content=True)
                finally:
                    image.unlink(missing_ok=True)
            else:
                markdown = local_markdown(page.text)
            markdown = structure.format_page(number, markdown)
            info = {"page": number, "parser": route, "reasons": page.reasons,
                    "uncertain": uncertain, "uncertainty_reasons": uncertainty_reasons, "characters": len(markdown), **({"figures": figures} if figures else {}),
                    "output_repairs": sorted(set(output_repairs)),
                    "duration_ms": round((time.monotonic() - started) * 1000)}
            if fallback_error:
                info.update(fallback_error=fallback_error, missing_content=not any(c.isalpha() for c in markdown),
                            missing_visual_content=True)
            temporary = checkpoint.with_suffix(".tmp")
            temporary.write_text(json.dumps({"version": cache_version, "info": info,
                                             "native": page.text, "markdown": markdown}, ensure_ascii=False))
            temporary.replace(checkpoint)
            finished[number] = info
            event("page_finished", task_id=task_id, **info)
        progress({"page_count": page_count, "processed_pages": len(finished),
                  "pages": [finished[n] for n in sorted(finished)],
                  "model_calls": model.calls, "usage": model.usage})

    async def worker():
        for number in numbers:
            # Share the global page limit across documents. Native
            # extraction and rendering also have independent small CPU limits.
            async with model.page_limiter:
                await read_page(number)

    workers = [asyncio.create_task(worker(), name=f"{task_id}-page-worker-{n}")
               for n in range(min(page_count, settings.page_concurrency))]
    try:
        await asyncio.gather(*workers)
    except BaseException:
        # gather alone leaves siblings running after a page error. Cancel and await
        # them before releasing the document slot or deleting its source PDF.
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        raise

    assembly_started = time.monotonic()
    content_pages = 0
    with (source.parent / "raw.txt").open("w", encoding="utf-8") as raw, \
            (source.parent / "knowledge.md").open("w", encoding="utf-8") as knowledge:
        for number in range(1, page_count + 1):
            cached = json.loads((source.parent / f"checkpoint-{number:05}.json").read_text())
            raw.write(f"\f{cached['native']}\n")
            if cached["markdown"] or cached["info"].get("figures"):
                if content_pages:
                    knowledge.write("\n\n---\n\n")
                knowledge.write(cached["markdown"])
                for figure in cached["info"].get("figures", []):
                    knowledge.write(f"\n\n### AI diagram interpretation (PDF page {number})\n\n")
                    knowledge.write("\n".join(f"- {step}" for step in figure["steps"]))
                    knowledge.write(f"\n\n<!-- docbreaker-figure:{figure['name']} -->")
                content_pages += 1
        knowledge.write("\n")
    if not content_pages:
        fallback = next((p for p in finished.values() if p.get("fallback_error")), None)
        if fallback:
            raise ParseError(fallback["fallback_error"], "No readable content was extracted; model extraction failed "
                             "and the PDF has no usable native text. Retry can re-read the missing pages")
        raise ParseError("empty_document", "No readable content was extracted from this PDF")
    pages = [finished[n] for n in sorted(finished)]
    event("stage_finished", task_id=task_id, stage="assemble",
          duration_ms=round((time.monotonic() - assembly_started) * 1000))
    event("document_routes", task_id=task_id, pages=page_count,
          local_pages=sum(p["parser"] == "local" for p in pages),
          model_pages=sum(p["parser"] in {"llm", "hybrid"} for p in pages),
          blank_pages=sum(p["parser"] == "blank" for p in pages), model_calls=model.calls)
    warnings = []
    degraded = [p for p in pages if p["parser"] == "local_fallback"]
    missing = [p["page"] for p in degraded if p.get("missing_content")]
    if degraded:
        warnings.append("Model extraction failed on pages " + ", ".join(str(p["page"]) for p in degraded)
                        + "; native text only was retained; visual text and diagrams may be missing")
    if missing:
        warnings.append("No substantive native text could be extracted on pages " + ", ".join(map(str, missing)))
    if any(p["uncertain"] for p in pages):
        warnings.append("Some page values remain illegible; review flagged pages")
    if any(p.get("output_repairs") for p in pages):
        warnings.append("Model output formatting was normalized; see pages[].output_repairs")
    return {"page_count": page_count, "processed_pages": page_count, "pages": pages,
            "model_calls": model.calls, "usage": model.usage, "stage": "completed", "current_page": None,
            "failed_pages": [], "degraded_pages": [{"page": p["page"], "code": p["fallback_error"],
                                                       "missing_content": p.get("missing_content", False),
                                                       "missing_visual_content": True} for p in degraded],
            "missing_content_pages": missing, "warnings": warnings}
