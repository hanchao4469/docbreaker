"""Durable local queue, ten bounded workers, resumable per-page processing."""

import asyncio
import fcntl
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

import httpx

from .config import Settings
from .concurrency import ConcurrencyLimiter
from .events import ParseError, event
from .model_client import ModelClient
from .parser import parse_pdf

TASK_ID = re.compile(r"task_[a-f0-9]{32}\Z")
TERMINAL = {"completed", "failed"}
RETRYABLE = {"task_timeout", "queue_timeout", "model_network_error", "model_unavailable", "worker_stopped",
             "invalid_model_output", "empty_model_output", "model_incomplete"}


def write_json(path: Path, value: dict):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class Jobs:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.capacity = settings.queue_size + settings.concurrency
        self.queue = asyncio.Queue(maxsize=self.capacity)
        self.active = 0
        self.uploading = 0
        self.current = set()
        self.workers = []
        self.worker = None  # compatibility for callers checking the first worker
        self.janitor = self.client = self.lock = None
        self.ready = False
        self.accepting = True
        self.idempotency = {}
        self.model_limiter = ConcurrencyLimiter(settings.model_concurrency)
        self.page_limiter = ConcurrencyLimiter(max(settings.page_concurrency, settings.model_concurrency))
        self.native_limiter = asyncio.Semaphore(settings.render_concurrency)
        self.render_limiter = asyncio.Semaphore(settings.render_concurrency)

    @property
    def healthy(self):
        return self.ready and len(self.workers) == self.settings.concurrency and all(not w.done() for w in self.workers)

    async def start(self):
        root = self.settings.data_dir
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = (root / ".worker.lock").open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("Only one DocBreaker API process may use this data directory") from None
        self.cleanup()
        pending = []
        for directory in root.glob("task_*"):
            if not directory.is_dir() or not TASK_ID.fullmatch(directory.name):
                continue
            try:
                state = json.loads((directory / "run.json").read_text())
            except FileNotFoundError:
                shutil.rmtree(directory)
                continue
            except (ValueError, OSError):
                event("state_corrupted", task_id=directory.name)
                continue
            if state.get("status") not in TERMINAL:
                if state.get("status") != "uploading" and (directory / "source.pdf").is_file():
                    pending.append((state.get("created_at", 0), directory.name))
                else:
                    self.update(directory.name, status="failed", completed_at=time.time(),
                                idempotency_digest=None,
                                error={"code": "worker_restarted", "message": "Upload was interrupted; submit again"})
                    (directory / "source.pdf").unlink(missing_ok=True)
                    # A half-upload must not permanently occupy its idempotency key.
                    state.pop("idempotency_digest", None)
            if state.get("idempotency_digest"):
                self.idempotency[state["idempotency_digest"]] = directory.name
            for image in directory.glob("page-*.jpg"):
                image.unlink(missing_ok=True)
        self.client = httpx.AsyncClient(follow_redirects=False, trust_env=False,
                                        limits=httpx.Limits(max_connections=self.settings.model_concurrency,
                                                            max_keepalive_connections=self.settings.model_concurrency))
        self.queue = asyncio.Queue(maxsize=max(self.capacity, len(pending)))
        for _, task_id in sorted(pending):
            self.update(task_id, status="queued", queued_at=time.time(),
                        recovered=True, error=None)
            # Restoring a previously larger queue must never discard accepted PDFs.
            self.queue.put_nowait(task_id)
            self.active += 1
            event("task_recovered", task_id=task_id)
        self.workers = [asyncio.create_task(self.work(), name=f"pdf-worker-{i}")
                        for i in range(self.settings.concurrency)]
        self.worker = self.workers[0]
        self.janitor = asyncio.create_task(self.maintain(), name="retention")
        self.ready = True
        event("service_started", build=self.settings.build, concurrency=self.settings.concurrency,
              model_concurrency=self.settings.model_concurrency, queue_limit=self.settings.queue_size,
              page_concurrency=self.settings.page_concurrency,
              recovered=len(pending), model_configured=bool(self.settings.litellm_api_key), model=self.settings.model)

    async def stop(self):
        self.ready = False
        tasks = [*self.workers, self.janitor]
        for task in tasks:
            if task:
                task.cancel()
        await asyncio.gather(*(t for t in tasks if t), return_exceptions=True)
        if self.client:
            await self.client.aclose()
        if self.lock:
            self.lock.close()

    def folder(self, task_id: str) -> Path:
        if not TASK_ID.fullmatch(task_id):
            raise FileNotFoundError()
        path = self.settings.data_dir / task_id
        if not path.is_dir():
            raise FileNotFoundError()
        return path

    def read(self, task_id: str) -> dict:
        return json.loads((self.folder(task_id) / "run.json").read_text())

    def update(self, task_id: str, **fields):
        state = self.read(task_id)
        state.update(fields)
        write_json(self.folder(task_id) / "run.json", state)

    def cleanup(self):
        cutoff = time.time() - self.settings.retention_seconds
        for state_file in self.settings.data_dir.glob("task_*/run.json"):
            try:
                state = json.loads(state_file.read_text())
                if state.get("status") in TERMINAL and state.get("completed_at", time.time()) < cutoff:
                    shutil.rmtree(state_file.parent)
                    digest = state.get("idempotency_digest")
                    if digest:
                        self.idempotency.pop(digest, None)
                    event("task_expired", task_id=state_file.parent.name)
            except (OSError, ValueError):
                event("retention_error", error_type="state_or_filesystem")

    async def maintain(self):
        while True:
            await asyncio.sleep(60)
            self.cleanup()

    def digest(self, key, tenant_id=None):
        # Preserve legacy unscoped keys while isolating identical keys per tenant.
        value = json.dumps([tenant_id, key]) if tenant_id is not None else key
        return hashlib.sha256(value.encode()).hexdigest() if key else None

    def find_idempotent(self, key, tenant_id=None):
        task_id = self.idempotency.get(self.digest(key, tenant_id))
        return self.read(task_id) if task_id else None

    def reserve(self, filename: str, key=None, tenant_id: int | None = None, figures: bool = False) -> tuple[str, Path]:
        if tenant_id is not None and (type(tenant_id) is not int or not 0 < tenant_id <= 2**63 - 1):
            raise ParseError("invalid_tenant_id", "tenant_id must be a positive 64-bit integer")
        if not self.accepting:
            raise ParseError("service_draining", "New tasks are paused; no PDF task was accepted")
        if not self.healthy:
            raise ParseError("not_ready", "PDF worker is not ready")
        if self.active >= self.capacity or self.uploading >= self.settings.upload_concurrency:
            raise ParseError("queue_full", "PDF queue is full; retry later")
        used = sum(f.stat().st_size for f in self.settings.data_dir.glob("task_*/*") if f.is_file())
        if used + (self.uploading + 1) * self.settings.max_upload_bytes > self.settings.max_disk_bytes:
            raise ParseError("storage_full", "Result storage is full; retry after retention cleanup")
        self.active += 1
        self.uploading += 1
        task_id = "task_" + uuid.uuid4().hex
        directory = self.settings.data_dir / task_id
        digest = self.digest(key, tenant_id)
        try:
            directory.mkdir(mode=0o700)
            write_json(directory / "run.json", {"task_id": task_id, "filename": filename, "tenant_id": tenant_id,
                       "status": "uploading", "extract_figures": figures, "created_at": time.time(), "build": self.settings.build,
                       "idempotency_digest": digest, "processed_pages": 0, "model_calls": 0,
                       "processing_ms_total": 0, "pages": [], "usage": {}})
            if digest:
                self.idempotency[digest] = task_id
        except BaseException:
            self.active -= 1
            self.uploading -= 1
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return task_id, directory

    def abandon(self, task_id: str):
        state = self.read(task_id)
        if state.get("idempotency_digest"):
            self.idempotency.pop(state["idempotency_digest"], None)
        shutil.rmtree(self.folder(task_id))
        self.active -= 1
        self.uploading -= 1

    def submit(self, task_id: str, size: int, sha256=None):
        self.update(task_id, status="queued", queued_at=time.time(), file_bytes=size, source_sha256=sha256)
        self.queue.put_nowait(task_id)
        self.uploading -= 1
        event("task_queued", task_id=task_id, file_bytes=size, queue_depth=self.queue.qsize())

    def retry(self, task_id):
        state = self.read(task_id)
        if state["status"] != "failed" or (state.get("error") or {}).get("code") not in RETRYABLE:
            raise ParseError("not_retryable", "Only failed transient tasks can be retried")
        if not (self.folder(task_id) / "source.pdf").is_file():
            raise ParseError("source_expired", "Source PDF expired; submit again")
        if not self.accepting:
            raise ParseError("service_draining", "New tasks are paused; no PDF task was accepted")
        if not self.healthy:
            raise ParseError("not_ready", "PDF worker is not ready")
        if self.active >= self.capacity:
            raise ParseError("queue_full", "PDF queue is full; retry later")
        self.update(task_id, status="queued", queued_at=time.time(), error=None,
                    processing_ms_total=0, build=self.settings.build,
                    retry_count=state.get("retry_count", 0) + 1)
        self.active += 1
        self.queue.put_nowait(task_id)
        event("task_retried", task_id=task_id)

    async def work(self):
        while True:
            task_id = await self.queue.get()
            self.current.add(task_id)
            started = time.monotonic()
            directory = self.folder(task_id)
            state = self.read(task_id)
            previous_ms = state.get("processing_ms_total", 0)
            cancelled = False
            try:
                queued_s = time.time() - state.get("queued_at", state["created_at"])
                queued_ms = round(queued_s * 1000)
                if queued_s > self.settings.queue_timeout:
                    raise ParseError("queue_timeout", "PDF exceeded the queue wait limit")
                self.update(task_id, status="running", started_at=time.time(), queued_ms=queued_ms,
                            build=self.settings.build)
                event("task_started", task_id=task_id, tenant_id=state.get("tenant_id"), queued_ms=queued_ms)
                loop = asyncio.get_running_loop()
                async with asyncio.timeout(max(.001, self.settings.task_timeout - previous_ms / 1000)) as deadline:
                    def progress(fields):
                        # Inspection supplies the actual page count before any model request.
                        if "page_count" in fields:
                            budget = min(self.settings.task_timeout,
                                         max(self.settings.min_task_timeout, fields["page_count"] * self.settings.page_budget + 60))
                            deadline.reschedule(loop.time() + max(.001, budget - previous_ms / 1000 - (time.monotonic() - started)))
                        fields["processing_ms_total"] = previous_ms + round((time.monotonic() - started) * 1000)
                        self.update(task_id, **fields)
                    model = ModelClient(self.settings, self.client, on_attempt=progress,
                                        limiter=self.model_limiter, render_limiter=self.render_limiter,
                                        page_limiter=self.page_limiter, native_limiter=self.native_limiter, tenant_id=state.get("tenant_id"), figures=state.get("extract_figures", False))
                    model.calls, model.usage = state.get("model_calls", 0), dict(state.get("usage", {}))
                    model.stats = dict(state.get("model_stats", {}))
                    result = await parse_pdf(directory / "source.pdf", state["filename"], task_id,
                                             self.settings, model, progress)
                self.update(task_id, status="completed", completed_at=time.time(), **result,
                            duration_ms=round((time.monotonic() - started) * 1000))
                event("task_completed", task_id=task_id, tenant_id=state.get("tenant_id"), duration_ms=round((time.monotonic() - started) * 1000),
                      page_count=result["page_count"], model_calls=result["model_calls"], usage=result["usage"])
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    cancelled = True
                    self.update(task_id, status="queued", processing_ms_total=previous_ms + round((time.monotonic() - started) * 1000))
                    raise
                if isinstance(exc, ParseError):
                    code, message = exc.code, exc.message
                elif isinstance(exc, TimeoutError):
                    code, message = "task_timeout", "PDF exceeded its processing budget; completed pages can be resumed"
                elif isinstance(exc, Exception):
                    code, message = "internal_error", "PDF processing failed; contact the operator with the task ID"
                else:
                    raise
                self.update(task_id, status="failed", completed_at=time.time(),
                            error={"code": code, "message": message},
                            duration_ms=round((time.monotonic() - started) * 1000))
                event("task_failed", task_id=task_id, code=code, error_type=type(exc).__name__,
                      duration_ms=round((time.monotonic() - started) * 1000))
            finally:
                for image in directory.glob("page-*.jpg"):
                    image.unlink(missing_ok=True)
                final = self.read(task_id)
                if not cancelled and (final["status"] == "completed" or (final.get("error") or {}).get("code") not in RETRYABLE):
                    (directory / "source.pdf").unlink(missing_ok=True)
                self.active -= 1
                self.current.discard(task_id)
                self.queue.task_done()
