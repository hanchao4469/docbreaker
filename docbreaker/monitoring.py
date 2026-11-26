"""Read-only, retention-scoped telemetry; disk scans run outside the API event loop."""

from collections import Counter, defaultdict
import json
import math
import os
from pathlib import Path
import shutil
import threading
import time


STATUSES = ("uploading", "queued", "running", "completed", "failed")


def numeric(value, default=0):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else default


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values)*fraction)-1)] if values else None


class SystemSampler:
    def __init__(self, proc=Path("/proc"), cgroup=Path("/sys/fs/cgroup")):
        self.proc, self.cgroup = proc, cgroup
        self.previous_host = self.previous_container = None

    def read(self, path):
        try:
            return path.read_text()
        except (OSError, ValueError):
            return ""

    def sample(self, data_dir):
        now = time.monotonic()
        host_cpu = container_cpu = container_capacity = None
        try:
            ticks = list(map(int, self.read(self.proc/"stat").splitlines()[0].split()[1:9]))
            total, idle = sum(ticks), ticks[3]+ticks[4]
            if self.previous_host:
                dt, di = total-self.previous_host[0], idle-self.previous_host[1]
                host_cpu = max(0, min(100, (dt-di)/dt*100)) if dt > 0 else None
            self.previous_host = total, idle
        except (ValueError, IndexError):
            pass
        try:
            cpu_stat = dict(line.split() for line in self.read(self.cgroup/"cpu.stat").splitlines())
            usage = int(cpu_stat["usage_usec"])/1e6
        except (KeyError, ValueError):
            try:
                usage = int(self.read(self.cgroup/"cpuacct/cpuacct.usage"))/1e9
            except ValueError:
                usage = None
        cores = os.cpu_count() or 1
        try:
            quota, period = self.read(self.cgroup/"cpu.max").split()
            if quota != "max":
                cores = min(cores, int(quota)/int(period))
        except (ValueError, ZeroDivisionError):
            try:
                quota = int(self.read(self.cgroup/"cpu/cpu.cfs_quota_us"))
                period = int(self.read(self.cgroup/"cpu/cpu.cfs_period_us"))
                if quota > 0 and period > 0:
                    cores = min(cores, quota/period)
            except ValueError:
                pass
        if usage is not None:
            if self.previous_container:
                elapsed, consumed = now-self.previous_container[0], usage-self.previous_container[1]
                if elapsed > 0 and consumed >= 0:
                    container_cpu = consumed/elapsed*100  # 100% = one CPU core.
                    container_capacity = min(100, container_cpu/cores) if cores > 0 else None
            self.previous_container = now, usage
        memory = {}
        for line in self.read(self.proc/"meminfo").splitlines():
            parts = line.split()
            if len(parts) >= 2:
                try:
                    memory[parts[0].rstrip(":")] = int(parts[1])*1024
                except ValueError:
                    pass
        host_total, host_available = memory.get("MemTotal"), memory.get("MemAvailable")
        def integer(path):
            try:
                return int(self.read(path))
            except ValueError:
                return None
        used = integer(self.cgroup/"memory.current")
        limit = integer(self.cgroup/"memory.max")
        if used is None:
            used = integer(self.cgroup/"memory/memory.usage_in_bytes")
            limit = integer(self.cgroup/"memory/memory.limit_in_bytes")
        # cgroup v1 uses a huge sentinel when no hard memory limit is set.
        if limit is not None and (limit >= 2**60 or limit <= 0):
            limit = None
        effective_limit = min(host_total, limit) if host_total and limit else limit or host_total
        try:
            rss = int(self.read(self.proc/"self/statm").split()[1])*os.sysconf("SC_PAGE_SIZE")
        except (ValueError, IndexError, OSError):
            rss = None
        try:
            disk = shutil.disk_usage(data_dir)
            disk_usage = {"total_bytes": disk.total, "used_bytes": disk.used, "free_bytes": disk.free}
        except OSError:
            disk_usage = {"total_bytes": None, "used_bytes": None, "free_bytes": None}
        try:
            load = list(os.getloadavg())
        except OSError:
            load = None
        return {"host_cpu_percent": host_cpu, "container_cpu_percent": container_cpu,
                "container_cpu_capacity_percent": container_capacity, "cpu_cores": cores,
                "host_memory_total_bytes": host_total, "host_memory_available_bytes": host_available,
                "container_memory_bytes": used, "container_memory_limit_bytes": limit,
                "container_memory_capacity_bytes": effective_limit, "process_rss_bytes": rss,
                "disk": disk_usage, "load_average": load}


class Monitor:
    def __init__(self, jobs, cache_seconds=3):
        self.jobs, self.cache_seconds = jobs, cache_seconds
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.cached_at = 0
        self.cache = None
        self.records = []
        self.files = {}
        self.system = SystemSampler()

    def refresh(self):
        now = time.time()
        if self.cache is not None and time.monotonic()-self.cached_at < self.cache_seconds:
            return
        records, invalid, present = [], 0, set()
        for path in self.jobs.settings.data_dir.glob("task_*/run.json"):
            try:
                stat = path.stat()
                if stat.st_size > 2*1024**2:
                    invalid += 1
                    continue
                stamp = stat.st_mtime_ns, stat.st_size
                cached = self.files.get(path)
                state = cached[1] if cached and cached[0] == stamp else json.loads(path.read_text())
                if not isinstance(state, dict) or state.get("status") not in STATUSES:
                    invalid += 1
                    continue
                self.files[path] = stamp, state
                present.add(path)
                records.append(state)
            except FileNotFoundError:
                continue  # Retention cleanup can race a read.
            except (OSError, ValueError):
                invalid += 1
        self.files = {p: value for p, value in self.files.items() if p in present}
        self.records = records
        self.cache = self.aggregate(records, now, invalid)
        self.cache["system"] = self.system.sample(self.jobs.settings.data_dir)
        self.cached_at = time.monotonic()

    def aggregate(self, records, now, invalid):
        statuses = Counter()
        tenants = {}
        usage = Counter({name:0 for name in ("prompt_tokens", "completion_tokens", "total_tokens")})
        model = Counter({name:0 for name in ("calls", "observed_attempts", "success", "retries", "failed", "duration_ms_total")})
        model_errors = Counter()
        durations, waits, queue_ages = [], [], []
        errors, recent = [], []
        local = visual = blank = retries = 0
        for state in records:
            status = state["status"]
            statuses[status] += 1
            tenant_id = state.get("tenant_id")
            key = str(tenant_id) if tenant_id is not None else "unassigned"
            tenant = tenants.setdefault(key, {"tenant_id": tenant_id, "total": 0, **{s: 0 for s in STATUSES},
                                              "pages": 0, "processed_pages": 0, "model_calls": 0,
                                              "total_tokens": 0})
            tenant["total"] += 1
            tenant[status] += 1
            for name in ("processed_pages", "model_calls"):
                tenant[name] += numeric(state.get(name))
            tenant["pages"] += numeric(state.get("page_count"))
            tokens = state.get("usage") or {}
            for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                usage[name] += numeric(tokens.get(name))
            tenant["total_tokens"] += numeric(tokens.get("total_tokens"))
            stats = state.get("model_stats") or {}
            for name in ("observed_attempts", "success", "retries", "failed", "duration_ms_total"):
                model[name] += numeric(stats.get(name))
            model_errors.update({code: numeric(count) for code,count in (stats.get("errors") or {}).items()})
            model["calls"] += numeric(state.get("model_calls"))
            for page in state.get("pages") or []:
                route = page.get("parser")
                local += route == "local"
                visual += route in {"llm", "hybrid", "local_fallback"}
                blank += route == "blank"
            retries += numeric(state.get("retry_count"))
            if status == "queued":
                queue_ages.append(max(0, now-numeric(state.get("queued_at"),now)))
            if status == "completed":
                durations.append(numeric(state.get("duration_ms")))
                waits.append(numeric(state.get("queued_ms")))
                if numeric(state.get("completed_at")) >= now-300:
                    recent.append(state)
            if status == "failed":
                error = state.get("error") or {}
                errors.append({"task_id": state.get("task_id"), "tenant_id": tenant_id,
                               "filename": state.get("filename"), "code": error.get("code", "unknown"),
                               "completed_at": state.get("completed_at")})
        terminal = statuses["completed"]+statuses["failed"]
        timeline = []
        # Fixed five-minute bins, including the current partial bin, over the last hour.
        first = int(now//300)*300-11*300
        for index in range(12):
            at = first+index*300
            completed = [r for r in records if r["status"] == "completed" and at <= numeric(r.get("completed_at")) < at+300]
            failed = sum(r["status"] == "failed" and at <= numeric(r.get("completed_at")) < at+300 for r in records)
            timeline.append({"at": at, "completed": len(completed), "failed": failed,
                             "pages": sum(numeric(r.get("page_count")) for r in completed)})
        return {"sampled_at": now, "scope": {"kind": "retained_tasks", "retention_seconds": self.jobs.settings.retention_seconds,
                "oldest_created_at": min((numeric(s.get("created_at"),now) for s in records), default=None),
                "invalid_state_files": invalid},
                "totals": {"total": len(records), **{s: statuses[s] for s in STATUSES},
                           "success_rate": statuses["completed"]/terminal if terminal else None,
                           "pages": sum(t["pages"] for t in tenants.values()),
                           "processed_pages": sum(t["processed_pages"] for t in tenants.values()),
                           "local_pages": local, "model_pages": visual, "blank_pages": blank, "task_retries": retries,
                           "degraded_pages": sum(len(r.get("degraded_pages") or []) for r in records),
                           "missing_content_pages": sum(len(r.get("missing_content_pages") or []) for r in records)},
                "performance": {"window_seconds": 300, "completed_last_5m": len(recent),
                                "docs_per_minute": len(recent)/5,
                                "pages_per_minute": sum(numeric(s.get("page_count")) for s in recent)/5,
                                "average_processing_ms": sum(durations)/len(durations) if durations else None,
                                "p95_processing_ms": percentile(durations,.95),
                                "average_queue_ms": sum(waits)/len(waits) if waits else None,
                                "oldest_queue_seconds": max(queue_ages,default=0)},
                "model": {**dict(model), "errors": dict(model_errors), "usage": dict(usage),
                          "average_attempt_ms": model["duration_ms_total"]/model["observed_attempts"] if model["observed_attempts"] else None},
                "tenants": sorted(tenants.values(), key=lambda t: (-(t["running"]+t["queued"]+t["uploading"]),-t["failed"],-t["total"])),
                "recent_errors": sorted(errors,key=lambda s:numeric(s["completed_at"]),reverse=True)[:10],
                "timeline": timeline}

    def snapshot(self):
        with self.lock:
            self.refresh()
            jobs, settings = self.jobs, self.jobs.settings
            # Runtime counters are read live, while retained-history scans are cached.
            return {**self.cache, "service": {"healthy": jobs.healthy, "accepting": jobs.accepting,
                    "build": settings.build, "model": settings.model,
                    "model_configured": bool(settings.litellm_api_key), "uptime_seconds": time.monotonic()-self.started,
                    "running": len(jobs.current), "queued": jobs.queue.qsize(), "uploading": jobs.uploading,
                    "document_limit": settings.concurrency, "queue_limit": settings.queue_size,
                    "outstanding": jobs.active,
                    "safe_to_deploy": (not jobs.accepting and jobs.active == 0 and jobs.queue.empty()
                                       and not jobs.current and jobs.uploading == 0),
                    "model_in_flight": jobs.model_limiter.active, "model_waiting": jobs.model_limiter.waiting,
                    "model_limit": settings.model_concurrency, "model_peak": jobs.model_limiter.peak,
                    "pages_in_flight": jobs.page_limiter.active, "page_limit": jobs.page_limiter.limit}}

    def tasks(self, tenant=None, status=None, offset=0, limit=25):
        with self.lock:
            self.refresh()
            records = [r for r in self.records
                       if (status is None or r["status"] == status)
                       and (tenant is None or (str(r.get("tenant_id")) if r.get("tenant_id") is not None else "unassigned") == tenant)]
            records.sort(key=lambda r: numeric(r.get("created_at")),reverse=True)
            keys = ("task_id", "tenant_id", "filename", "status", "stage", "created_at", "started_at", "completed_at",
                    "page_count", "processed_pages", "model_calls", "duration_ms", "queued_ms", "retry_count")
            items = []
            for state in records[offset:offset+limit]:
                item = {key:state.get(key) for key in keys}
                item["total_tokens"] = numeric((state.get("usage") or {}).get("total_tokens"))
                item["error_code"] = (state.get("error") or {}).get("code")
                items.append(item)
            return {"total": len(records), "offset": offset, "limit": limit, "items": items}
