"""Independent persistent web queue; PDF admission and worker slots stay separate."""
import asyncio
import json
import logging
import re
import shutil
import time
import uuid

from .events import event
from .jobs import write_json
from .web_crawl import crawl
from .web_fetch import Fetcher, WebError
from .web_enrich import WebEnricher

WEB_SERVICE_ID = "web-parse"
TASK = re.compile(r"web_[a-f0-9]{32}\Z")
TERMINAL = {"completed", "partial", "failed"}
CAPACITY = 12
RESULT_RESERVATION = 26_000_000
STORAGE_LIMIT = 256_000_000


class WebJobs:
    def __init__(self, settings):
        self.settings = settings
        self.root = settings.data_dir / "web"
        self.queue = asyncio.Queue()
        self.active = set()
        self.workers = []
        self.running = 0

    def path(self, task_id):
        if not TASK.fullmatch(task_id):
            raise FileNotFoundError()
        return self.root / task_id

    def read(self, task_id):
        return json.loads((self.path(task_id) / "state.json").read_text())

    def update(self, state, **fields):
        state.update(fields)
        write_json(self.path(state["task_id"]) / "state.json", state)

    async def start(self):
        self.root.mkdir(exist_ok=True, mode=0o700)
        self.fetcher = Fetcher()
        self.enricher = WebEnricher(self.settings, self.fetcher, WEB_SERVICE_ID)
        self.prune()
        pending = []
        for f in self.root.glob("web_*/state.json"):
            try:
                state = self.read(f.parent.name)
                if state["status"] not in TERMINAL:
                    pending.append(state)
            except (OSError, ValueError, KeyError):
                event("web_state_invalid", task_id=f.parent.name)
        for state in sorted(pending, key=lambda x: x["created_at"]):
            self.update(state, status="queued", attempted=0)
            self.enqueue(state)
        self.workers = [asyncio.create_task(self.work(), name=f"web-worker-{i}") for i in range(2)]
        self.janitor = asyncio.create_task(self.maintain(), name="web-retention")

    async def stop(self):
        for w in [*self.workers, self.janitor]:
            w.cancel()
        await asyncio.gather(*self.workers, self.janitor, return_exceptions=True)
        await self.enricher.close()
        await self.fetcher.close()

    def prune(self):
        cutoff = time.time() - self.settings.retention_seconds
        for f in self.root.glob("web_*/state.json"):
            try:
                state = self.read(f.parent.name)
                if state["status"] in TERMINAL and state["completed_at"] < cutoff:
                    shutil.rmtree(f.parent)
            except (OSError, ValueError, KeyError):
                event("web_retention_error", task_id=f.parent.name)

    async def maintain(self):
        while True:
            await asyncio.sleep(60)
            self.prune()

    def enqueue(self, state):
        self.active.add(state["task_id"])
        self.queue.put_nowait(state)

    def submit(self, tenant, url, max_pages, *, full_site):
        if len(self.active) >= CAPACITY:
            raise WebError("web_queue_full")
        stored = sum(p.stat().st_size for p in self.root.glob("web_*/*") if p.is_file())
        if stored + (len(self.active) + 1) * RESULT_RESERVATION > STORAGE_LIMIT:
            raise WebError("web_storage_full")
        task_id = "web_" + uuid.uuid4().hex
        state = {"task_id": task_id, "tenant_id": tenant, "service_id": WEB_SERVICE_ID, "url": url, "max_pages": max_pages,
                 "isFullSiteCrawl": full_site, "attempted": 0, "status": "queued", "created_at": time.time(), "build": self.settings.build}
        self.path(task_id).mkdir(mode=0o700)
        try:
            self.update(state)
        except BaseException:
            shutil.rmtree(self.path(task_id), ignore_errors=True)
            raise
        self.enqueue(state)
        event("web_task_queued", task_id=task_id, tenant_id=tenant, service_id=WEB_SERVICE_ID,
              max_pages=max_pages, full_site=full_site)
        return state

    async def work(self):
        while True:
            state = await self.queue.get()
            self.running += 1
            tid, tenant = state["task_id"], state["tenant_id"]
            try:
                self.update(state, status="running", started_at=time.time())
                async with asyncio.timeout(600):
                    result = await crawl(self.fetcher, state["url"], state["max_pages"],
                                         lambda n: self.update(state, attempted=n),
                                         full_site=state.get("isFullSiteCrawl", True),
                                         enrich=self.enricher.for_task(tid, tenant, state.get("isFullSiteCrawl", True)))
                write_json(self.path(tid) / "result.json", result)
                good = result["counts"].get("extracted", 0)
                failures = sum(n for key, n in result["counts"].items()
                               if key not in {"extracted", "duplicate_content", "robots_denied"})
                incomplete = (failures or result["pending"] or result.get("pending_sitemaps")
                              or result.get("inventory_limited") or result.get("time_limit")
                              or any(item.get("error") not in {None, "http_404", "http_410", "robots_denied"}
                                     for item in result["discovery"]))
                status = ("partial" if incomplete else "completed") if good else "failed"
                self.update(state, status=status, counts=result["counts"], discovered=result["discovered"],
                            pending=result["pending"], completed_at=time.time())
                event("web_task_finished", task_id=tid, tenant_id=tenant,
                      service_id=state.get("service_id", WEB_SERVICE_ID), status=status,
                      counts=result["counts"], discovered=result["discovered"])
            except (WebError, TimeoutError) as exc:
                self.update(state, status="failed", error=getattr(exc, "code", "crawl_timeout"), completed_at=time.time())
            except Exception:
                logging.getLogger(__name__).exception("Web task failed: %s", tid)
                try:
                    self.update(state, status="failed", error="crawl_failed", completed_at=time.time())
                except OSError:
                    event("web_state_write_failed", task_id=tid)
            finally:
                self.active.discard(tid)
                self.running -= 1
                self.queue.task_done()
