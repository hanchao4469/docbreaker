# DocBreaker

**PDF and website extraction for RAG, with local-first processing and selective vision-model assistance.**

面向知识库的文档处理服务：普通 PDF 页优先本地提取；扫描件、复杂版面和图解按需调用视觉模型。网页从 HTML 中提取正文与表格，并可使用模型补充图片里的信息。

Early public release. Extraction and AI interpretation can contain errors; review important numbers and instructions against the source.

## What it does

- Extracts PDF text with Poppler; routes difficult pages to a configurable model through LiteLLM.
- Keeps page order and source languages; separates AI diagram interpretations from transcribed text.
- Exports selected diagram crops, with a second model pass to check their interpretation. Unverified instructions are withheld while the image remains available.
- Extracts website text, headings, links and tables; supports a selected URL or a bounded same-site crawl.
- Optionally transcribes selected informative web images using a separate model credential.
- Runs persistent asynchronous tasks with bounded document, page, rendering and model concurrency, retries and page-level recovery.
- Provides an authenticated operations dashboard at `/dashboard`, plus drain/resume controls.

## How it works

```text
PDF bytes → persistent queue → native text + layout inspection
                               ├─ reliable page → local extraction
                               └─ difficult page → rendered image + vision model
                                                    └─ optional diagram verification/crops
                            → ordered Markdown + per-page quality flags + usage

Web URL → public-address/scope checks → HTTP HTML fetch → main-content extraction
                                    → tables, links and structured facts
                                    → optional image transcription → result JSON
```

Website fetching uses HTTP and HTML parsing, not a browser engine. It does not execute JavaScript, log into sites, or bypass access challenges. See [limitations](docs/limitations.md).

## Quick start with Docker

Requires Docker. For model-assisted extraction, configure a LiteLLM gateway with access to a suitable vision model; local text extraction can run without it.

```sh
cp .env.example .env
# Edit .env: set your own DOCBREAKER_API_KEY (at least 32 characters).
# Configure your gateway URL, credentials and model alias when using AI.
docker compose up --build -d
```

Open <http://localhost:8000/dashboard> and enter your service API key. The default Compose port binding is local to your computer.

The gateway base URL must be a root URL, such as `https://gateway.example.com`, without `/v1`. Model calls use `/v1/chat/completions`, JSON output and `reasoning_effort=none`. Choose a model/gateway that supports these options; model availability and pricing depend on your provider.

`tenant_id` is a positive identifier provided by the caller. It is forwarded as `x-litellm-end-user-id`; PDF and web model calls use service labels `pdf-parse` and `web-parse`. This release uses a shared service API key, not separate tenant login credentials. Keep it behind a trusted application or authenticated gateway.

## Submit a PDF

Use raw PDF bytes, not multipart form data. Set `DOCBREAKER_API_KEY` in your shell to the same value as the server.

```sh
curl -X POST 'http://localhost:8000/v1/tasks?filename=manual.pdf&tenant_id=1&figures=true' \
  -H "Authorization: Bearer $DOCBREAKER_API_KEY" \
  -H 'Content-Type: application/pdf' \
  --data-binary @manual.pdf
```

The response contains `task_id` and a relative `result_url`. Poll that URL using the same Bearer header. A completed PDF result includes Markdown at `files[0].content.markdown`, exported figure names, page metadata, warnings and model usage.

Figure references use `<!-- docbreaker-figure:figure-N-M.jpg -->` markers. Download images through the authenticated figure endpoint and upload them to your own storage/CDN if you need durable HTTPS image links. **This standalone service does not include a CDN uploader.**

## Submit a web page

```sh
curl -X POST 'http://localhost:8000/v1/web/tasks' \
  -H "Authorization: Bearer $DOCBREAKER_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/","tenant_id":1,"isFullSiteCrawl":false}'
```

Poll the returned `result_url` with `?tenant_id=1`. To discover additional pages on the same site, use `"isFullSiteCrawl":true,"max_pages":30` (maximum 100). Respect the site's terms and crawl rules. Optional image enrichment uses `LITELLM_WEB_API_KEY`; without it, the HTML extraction path still works.

See [API reference](docs/api.md) and [.env.example](.env.example) for configuration.

## Run locally and test

Python 3.12+ and Poppler command-line tools are required.

```sh
# Debian/Ubuntu; on macOS use: brew install poppler
sudo apt-get update
sudo apt-get install -y poppler-utils poppler-data fonts-noto-cjk
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

For local operation, export environment values explicitly (the Python entry point does not load `.env` files) and set a writable data directory:

```sh
export DOCBREAKER_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export DOCBREAKER_DATA_DIR="$PWD/data"
python -m docbreaker
```

Tests generate small PDF/HTML fixtures and mock model requests; running the unit suite does not require real model credentials. To build the same tests in Docker:

```sh
docker build --target test -t docbreaker-tests .
```

## Operational boundaries

- Run one API process per data directory. Queue state and files are local; do not share a directory between multiple replicas.
- Default limits: 50 MiB per PDF, 200 pages, 10 document workers and 50 global PDF model slots. Tune down for small machines.
- Default PDF artifact retention is 24 hours. Persist the data volume, and copy results elsewhere for long-term storage.
- PDF page failures can degrade to native text; an accepted or completed task is not a guarantee of complete content. Inspect quality flags and warnings.
- Dashboard totals reflect retained PDF task records, not a permanent bill or a complete web-task analytics system. Calls that time out without returning usage cannot be fully costed from these totals.
- Drain rejects new tasks while accepted tasks finish. Wait for PDF and web work to empty before restarting; drain state resets on restart.

## Project layout

| Directory | Purpose |
|---|---|
| `docbreaker/` | API, queues, PDF routing, model transport and website extraction |
| `docbreaker/static/` | Operations dashboard (Chinese UI) |
| `tests/` | Generated-fixture and mocked-model regression tests |
| `deploy/` | Portable health and smoke checks |
| `tools/` | Load-test harness |
| `docs/` | Public API and limitations |

Contributions are welcome. Please include a reproducible, shareable fixture and test for extraction changes. Do not submit private documents or credentials in issues.

## License

Project code is licensed under [MIT](LICENSE). Dependencies and system tools retain their own licenses; see [third-party notices](THIRD_PARTY_NOTICES.md).
