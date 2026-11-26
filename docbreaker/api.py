"""Authenticated authenticated PDF submission, polling and diagnostics."""

import asyncio
import hashlib
import hmac
import json
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from .pdf_figures import FIGURE_NAME
from pydantic import BaseModel, ConfigDict, Field

from .config import Settings
from .events import ParseError, event
from .jobs import Jobs
from .monitoring import Monitor
from .memory import IdleMemoryReclaimer
from .web_fetch import WebError, normalize, private_path
from .web_jobs import TERMINAL as WEB_TERMINAL, WebJobs


class WebSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    url: str = Field(max_length=2048)
    tenant_id: int = Field(gt=0, lt=2**63)
    isFullSiteCrawl: bool = False
    max_pages: int | None = Field(default=None, ge=1, le=100)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    jobs = Jobs(settings)
    web = WebJobs(settings)
    monitor = Monitor(jobs)
    memory = IdleMemoryReclaimer(lambda: jobs.active == 0 and jobs.uploading == 0
                                and not web.active and jobs.page_limiter.active == 0
                                and jobs.model_limiter.active == 0)

    @asynccontextmanager
    async def lifespan(app):
        await jobs.start()
        try:
            await web.start()
            memory.start()
            try:
                yield
            finally:
                await memory.stop()
                await web.stop()
        finally:
            await jobs.stop()

    app = FastAPI(title="DocBreaker", version="1.0.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.jobs = jobs

    async def authenticate(request: Request):
        token = request.headers.get("authorization", "")
        if not hmac.compare_digest(token.encode(), ("Bearer " + settings.api_key).encode()):
            raise HTTPException(401, "Bearer authentication required", headers={"WWW-Authenticate": "Bearer"})

    @app.exception_handler(ParseError)
    async def parse_error(request, exc):
        status = {"queue_full": 429, "storage_full": 507, "not_ready": 503,
                  "not_retryable": 409, "source_expired": 410, "service_draining": 429}.get(exc.code, 400)
        headers = {"Retry-After": "5"} if status == 429 else {}
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=status, headers=headers)

    @app.get("/v1/health", dependencies=[Depends(authenticate)])
    async def health():
        ready = jobs.healthy
        return JSONResponse({"status": "healthy" if ready else "unhealthy", "service": "DocBreaker",
                             "build": settings.build, "concurrency": settings.concurrency,
                             "model_concurrency": settings.model_concurrency, "queued": jobs.queue.qsize(),
                             "model_in_flight": jobs.model_limiter.active,
                             "model_waiting": jobs.model_limiter.waiting,
                             "model_peak": jobs.model_limiter.peak,
                             "page_concurrency": settings.page_concurrency,
                             "page_global_limit": jobs.page_limiter.limit,
                             "pages_in_flight": jobs.page_limiter.active,
                             "queue_timeout_seconds": settings.queue_timeout,
                             "processing_timeout_seconds": settings.task_timeout,
                             "max_pages": settings.max_pages,
                             "running": len(jobs.current),
                             "uploading": jobs.uploading, "accepting": jobs.accepting,
                             "web_queued": web.queue.qsize(), "web_running": web.running,
                             "web_enabled": True,
                             "model_configured": bool(settings.litellm_api_key), "model": settings.model},
                            status_code=200 if ready else 503)

    @app.get("/dashboard")
    @app.get("/dashboard/", include_in_schema=False)
    async def dashboard():
        # The static login shell is public; all operational data requires Bearer auth.
        return Response((Path(__file__).parent/"static/dashboard.html").read_bytes(), media_type="text/html",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                 "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                                 "Referrer-Policy": "no-referrer"})

    @app.get("/dashboard/{asset}")
    async def dashboard_asset(asset: str):
        types = {"dashboard.js": "application/javascript", "dashboard.css": "text/css"}
        if asset not in types:
            raise HTTPException(404, "Asset not found")
        return Response((Path(__file__).parent/"static"/asset).read_bytes(), media_type=types[asset],
                        headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"})

    @app.get("/v1/dashboard/metrics", dependencies=[Depends(authenticate)])
    async def metrics():
        return JSONResponse(await asyncio.to_thread(monitor.snapshot), headers={"Cache-Control": "no-store"})

    @app.get("/v1/dashboard/tasks", dependencies=[Depends(authenticate)])
    async def dashboard_tasks(tenant: str | None = Query(None, pattern=r"^(?:[1-9][0-9]{0,18}|unassigned)$"),
                              status: Literal["uploading", "queued", "running", "completed", "failed"] | None = None,
                              offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100)):
        return JSONResponse(await asyncio.to_thread(monitor.tasks, tenant, status, offset, limit),
                            headers={"Cache-Control": "no-store"})

    @app.post("/v1/admin/drain", dependencies=[Depends(authenticate)])
    async def drain():
        jobs.accepting = False
        event("service_draining", active=jobs.active, web_active=len(web.active))
        return {"accepting": False, "active": jobs.active + len(web.active),
                "web_queued": web.queue.qsize(), "web_running": web.running, "queued": jobs.queue.qsize(),
                "running": len(jobs.current), "uploading": jobs.uploading}

    @app.post("/v1/admin/resume", dependencies=[Depends(authenticate)])
    async def resume():
        jobs.accepting = True
        event("service_resumed")
        return {"accepting": True}

    @app.post("/v1/web/tasks", status_code=202, dependencies=[Depends(authenticate)])
    async def submit_web(body: WebSubmission):
        if not jobs.healthy or not jobs.accepting:
            raise HTTPException(503, "Service is not accepting new tasks")
        url = normalize(body.url)
        if not url or private_path(url):
            raise HTTPException(400, "Expected a public HTTP(S) content URL")
        if not body.isFullSiteCrawl and body.max_pages not in (None, 1):
            raise HTTPException(400, "max_pages must be 1 for a single-page crawl")
        limit = (body.max_pages or 30) if body.isFullSiteCrawl else 1
        try:
            state = web.submit(body.tenant_id, url, limit, full_site=body.isFullSiteCrawl)
        except WebError as exc:
            raise HTTPException(507 if exc.code == "web_storage_full" else 429, exc.code) from None
        tid = state["task_id"]
        return {"task_id": tid, "status": "queued", "isFullSiteCrawl": body.isFullSiteCrawl,
                "result_url": f"/v1/web/tasks/{tid}/result"}

    @app.get("/v1/web/tasks/{task_id}/result", dependencies=[Depends(authenticate)])
    async def result_web(task_id: str, tenant_id: int = Query(ge=1, le=2**63 - 1)):
        try:
            state = web.read(task_id)
            if state["tenant_id"] != tenant_id:
                raise FileNotFoundError()
            output = web.path(task_id) / "result.json"
            if state["status"] in WEB_TERMINAL and output.is_file():
                return {**state, **json.loads(output.read_text())}
            return state
        except (FileNotFoundError, ValueError):
            raise HTTPException(404, "Web task not found or expired") from None

    @app.post("/v1/tasks", status_code=202, dependencies=[Depends(authenticate)])
    async def submit(request: Request, filename: str = Query("document.pdf", max_length=250),
                     tier: str = "basic", output_formats: str = "markdown",
                     tenant_id: int | None = Query(None, ge=1, le=2**63 - 1), figures: bool = False):
        if tier != "basic" or output_formats != "markdown":
            raise HTTPException(400, "Only tier=basic and output_formats=markdown are supported")
        content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type not in {"application/pdf", "application/octet-stream"}:
            raise HTTPException(415, "Send raw PDF bytes as application/pdf or application/octet-stream")
        if any(c in filename for c in "\r\n\x00") or "/" in filename or "\\" in filename:
            raise HTTPException(400, "filename must be a file name without a path")
        if not filename.lower().endswith(".pdf"):
            raise HTTPException(400, "Only PDF is supported")
        length = request.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > settings.max_upload_bytes):
            raise HTTPException(413, "PDF exceeds the upload size limit")
        key = request.headers.get("idempotency-key")
        if key and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
            raise HTTPException(400, "Invalid Idempotency-Key")
        expected_hash = request.headers.get("x-content-sha256")
        if expected_hash and not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
            raise HTTPException(400, "Invalid X-Content-SHA256")
        request_id = uuid.uuid4().hex
        started = time.monotonic()
        event("upload_started", request_id=request_id, tenant_id=tenant_id, declared_bytes=int(length) if length else None)

        async def receive(stream=None):
            size, header = 0, bytearray()
            digest = hashlib.sha256()
            async with asyncio.timeout(60):
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise HTTPException(413, "PDF exceeds the upload size limit")
                    digest.update(chunk)
                    if len(header) < 1024:
                        header.extend(chunk[:1024 - len(header)])
                    if stream:
                        stream.write(chunk)
            return size, header, digest.hexdigest()

        existing = jobs.find_idempotent(key, tenant_id) if key else None
        if existing:
            size, _, digest = await receive()
            # The original upload can finish while the duplicate request is drained.
            try:
                existing = jobs.read(existing["task_id"])
            except FileNotFoundError:
                return JSONResponse({"error": {"code": "upload_in_progress"}}, status_code=409,
                                    headers={"Retry-After": "1"})
            if existing["status"] == "uploading":
                return JSONResponse({"error": {"code": "upload_in_progress"}}, status_code=409,
                                    headers={"Retry-After": "1"})
            if (existing.get("source_sha256") != digest or existing["filename"] != filename
                    or (expected_hash and expected_hash != digest)):
                raise HTTPException(409, "Idempotency-Key was used for different content")
            event("upload_replayed", request_id=request_id, task_id=existing["task_id"],
                  file_bytes=size, duration_ms=round((time.monotonic() - started) * 1000))
            return {"task_id": existing["task_id"], "status": existing["status"], "replayed": True,
                    "result_url": f"/v1/tasks/{existing['task_id']}/result"}
        try:
            task_id, folder = jobs.reserve(filename, key, tenant_id, figures=figures)
        except ParseError as exc:
            # Finish bounded valid uploads before the response: early close/reset during
            # a large POST otherwise hides 429 from clients such as urllib and OkHttp.
            try:
                size, _, _ = await receive()
            except TimeoutError:
                raise HTTPException(408, "PDF upload timed out") from None
            event("upload_rejected", request_id=request_id, code=exc.code, file_bytes=size,
                  duration_ms=round((time.monotonic() - started) * 1000))
            raise
        try:
            with (folder / "source.pdf").open("wb") as stream:
                size, header, digest = await receive(stream)
            if b"%PDF-" not in header:
                raise HTTPException(400, "Input is not a PDF")
            if expected_hash and expected_hash != digest:
                raise HTTPException(400, "PDF checksum does not match X-Content-SHA256")
            jobs.submit(task_id, size, digest)
            event("upload_finished", request_id=request_id, task_id=task_id, file_bytes=size,
                  duration_ms=round((time.monotonic() - started) * 1000))
        except BaseException as exc:
            jobs.abandon(task_id)
            event("upload_failed", request_id=request_id, task_id=task_id, error_type=type(exc).__name__,
                  duration_ms=round((time.monotonic() - started) * 1000))
            if isinstance(exc, TimeoutError):
                raise HTTPException(408, "PDF upload timed out") from None
            raise
        # Relative URL resolves against exactly the caller's origin, without trusting Host/X-Forwarded-Host.
        return {"task_id": task_id, "status": "queued", "result_url": f"/v1/tasks/{task_id}/result"}

    @app.post("/v1/tasks/{task_id}/retry", status_code=202, dependencies=[Depends(authenticate)])
    async def retry(task_id: str):
        try:
            jobs.retry(task_id)
        except (FileNotFoundError, ValueError):
            raise HTTPException(404, "Task not found or expired") from None
        return {"task_id": task_id, "status": "queued", "result_url": f"/v1/tasks/{task_id}/result"}

    def lookup(task_id):
        try:
            return jobs.read(task_id)
        except (FileNotFoundError, ValueError):
            raise HTTPException(404, "Task not found or expired") from None

    @app.get("/v1/tasks/{task_id}/result", dependencies=[Depends(authenticate)])
    async def result(task_id: str):
        state = lookup(task_id)
        files = []
        if state["status"] == "completed":
            markdown = (jobs.folder(task_id) / "knowledge.md").read_text(encoding="utf-8")
            figures = [{"name": f["name"], "page": f["page"]}
                       for page in state.get("pages", []) for f in page.get("figures", [])]
            files = [{"filename": state["filename"], "content": {"markdown": markdown, "figures": figures}}]
        return {**state, "files": files}

    @app.get("/v1/tasks/{task_id}/figures/{name}", dependencies=[Depends(authenticate)])
    async def figure(task_id: str, name: str, tenant_id: int = Query(ge=1, le=2**63 - 1)):
        state = lookup(task_id)
        if (state.get("tenant_id") != tenant_id or state["status"] != "completed"
                or not FIGURE_NAME.fullmatch(name)
                or not any(f["name"] == name for page in state.get("pages", []) for f in page.get("figures", []))):
            raise HTTPException(404, "Figure not found")
        path = jobs.folder(task_id) / name
        if not path.is_file():
            raise HTTPException(404, "Figure not found")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})

    @app.get("/v1/tasks/{task_id}/artifacts/{name}", dependencies=[Depends(authenticate)])
    async def artifact(task_id: str, name: str):
        state = lookup(task_id)
        if name not in {"raw.txt", "knowledge.md", "run.json"}:
            raise HTTPException(404, "Artifact not found")
        if state["status"] not in {"completed", "failed"}:
            raise HTTPException(409, "Task is still processing")
        path = jobs.folder(task_id) / name
        if not path.is_file():
            raise HTTPException(404, "Artifact not found")
        return Response(path.read_bytes(), media_type="application/json" if name == "run.json" else "text/plain")

    return app
