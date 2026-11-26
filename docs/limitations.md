# Quality and deployment limitations

This is an early public snapshot, not a claim of lossless PDF conversion or universal website coverage.

## PDF fidelity

- Native text is used where the layout heuristics consider it reliable. Extraction normalizes layout into Markdown; whitespace, formatting and reading order can change.
- Vision OCR may omit or misread multilingual paragraphs, units, symbols and redacted/low-resolution text. The source language is requested, but exact character fidelity is not guaranteed.
- Diagram descriptions are model-generated interpretations, separately labeled. An additional model pass rejects uncertain instructions, but two model passes can still agree on a wrong interpretation. Verify safety-critical assembly instructions against the source image.
- Page-level model failure can fall back to whatever native text is available. This prevents one failed page from failing the entire file, but can leave a scanned page without usable content. Always inspect page quality metadata.
- Figure export is selective and budgeted (currently at most 24 crops per document), not a lossless export of every original image. Crops are rendered JPEGs.
- The service does not translate a whole document to one language or automatically upload figures to a CDN.

## Website coverage

- HTTP fetching and HTML parsing do not execute browser JavaScript or expand interactive content. Login-only content, anti-bot challenges, blocked requests and content loaded only through JavaScript may be unavailable.
- Bounded full-site discovery uses same-site links and sitemaps subject to scope, time, response-size and page limits. Query variants, external subdomains and attachment PDFs are not an exhaustive website mirror.
- Body selection, deduplication and structured-data recovery are heuristic. Some navigation can remain, and some relevant content can be omitted.
- Optional image transcription selects a bounded subset of informative same-site images, not every image on every CDN. It is not a general screenshot-based web agent.
- HTML table recovery preserves structure where possible and creates factual rows for retrieval. Merged cells and complex layouts still require validation.

## Operations and measurements

- Queue state is local to a single service process/data directory. This is not a distributed scheduler or a database-backed multi-node queue.
- A shared API key provides service-level access. Tenant IDs alone are not an end-user authorization boundary.
- Usage is based on tokens reported by the gateway; timed-out calls can incur charges without returned usage. Retention-scoped dashboard totals are not invoices.
- Performance depends on document mix, hardware, provider rate limits and model availability. Internal workload numbers are not shipped as independently reproducible public benchmark claims.
- Unit tests use generated fixtures and mocked model responses. They verify routing and service behavior, not broad real-world OCR accuracy. Add licensed public fixtures and independent scoring when evaluating quality.
