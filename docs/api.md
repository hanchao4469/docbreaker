# API reference

All endpoints below require `Authorization: Bearer <DOCBREAKER_API_KEY>`. The dashboard login shell and its static assets are public; operational data is authenticated.

## PDF

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/v1/tasks?filename=document.pdf&tenant_id=1&figures=true` | Submit raw PDF bytes; returns 202 and a relative result URL |
| GET | `/v1/tasks/{task_id}/result` | Poll task state and completed Markdown |
| POST | `/v1/tasks/{task_id}/retry` | Retry an eligible failed task while its source is retained |
| GET | `/v1/tasks/{task_id}/figures/{name}?tenant_id=1` | Download an exported JPEG; matching tenant required |
| GET | `/v1/tasks/{task_id}/artifacts/{name}` | Download `raw.txt`, `knowledge.md` or `run.json` for a terminal task |

The PDF request body is `application/pdf` or `application/octet-stream`. `tier=basic` and `output_formats=markdown` are the only supported values. `tenant_id` is needed for model calls; use an explicit identifier on every submission. `figures` defaults to false; enable it to export and describe selected diagrams.

Optional headers: `Idempotency-Key` (1–128 letters/digits/`_.:-`) and `X-Content-SHA256` (lowercase hex SHA-256). Reusing an idempotency key for different bytes or a different filename returns 409. A replay returns the existing task; it does not apply new extraction options.

PDF states: `uploading`, `queued`, `running`, `completed`, `failed`. Completed Markdown is `files[0].content.markdown`; figure names are `files[0].content.figures`. Page records include route, uncertainty, repairs and degradation metadata. Review warnings, `degraded_pages` and `missing_content_pages` before treating a result as complete. A fallback page can contain only recoverable native text.

Figure markers in Markdown identify the images listed in metadata; they are not public URLs. The authenticated JPEG endpoint is temporary, bounded by result retention. Durable image hosting and marker replacement belong in the integrating application.

## Web

`POST /v1/web/tasks` accepts JSON:

```json
{
  "url": "https://example.com/",
  "tenant_id": 1,
  "isFullSiteCrawl": false
}
```

Single-page mode accepts `max_pages` absent or equal to 1. Full-site mode defaults to 30 pages and accepts 1–100. It discovers a bounded set of same-site URLs; it does not promise every page on a website.

Poll `GET /v1/web/tasks/{task_id}/result?tenant_id=1`. A terminal result can be `completed`, `partial` or `failed`. Inspect the returned pages, attempts, errors and quality metadata; `partial` means the crawl did not finish without issues. Queue/state files for PDF and web are separate.

## Operations

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/v1/health` | Readiness, build, limits and PDF/web active counts |
| GET | `/v1/dashboard/metrics` | Resource samples and retained PDF task metrics |
| GET | `/v1/dashboard/tasks?offset=0&limit=25` | Paginated PDF task summaries; optional `tenant` and `status` filters |
| POST | `/v1/admin/drain` | Stop accepting new PDF and web work; existing work continues |
| POST | `/v1/admin/resume` | Resume task admission |

Drain is per process and not persisted across restart. Check both PDF and web queued/running work, as well as uploads, before shutdown. The caller should keep durable job records and retry rejected submissions with backoff. There is no built-in legacy-parser or CDN fallback service.

Defaults and validated ranges are defined in `docbreaker/config.py`. Web workers, crawl duration and some image/figure budgets also have implementation limits; the public release does not make all limits configurable.
