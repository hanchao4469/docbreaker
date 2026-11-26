"""Bounded PDF load test; run on the host, outside the measured container.

Read the existing API key from stdin, never command arguments or result files.
Ambiguous POST failures are replayed only with explicitly enabled idempotency.
"""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import hashlib
import http.client
import math
import os
from pathlib import Path
import random
import re
import resource
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


def percentile(values, percent):
    values = sorted(values)
    if not values:
        return None
    index = (len(values) - 1) * percent / 100
    low, high = math.floor(index), math.ceil(index)
    return round(values[low] + (values[high] - values[low]) * (index - low), 3)


def distribution(values):
    return {"count": len(values), "min": min(values) if values else None,
            "mean": round(sum(values) / len(values), 3) if values else None,
            "p50": percentile(values, 50), "p95": percentile(values, 95),
            "p99": percentile(values, 99), "max": max(values) if values else None}


def memory_bytes(value):
    match = re.fullmatch(r"\s*([\d.]+)\s*([A-Za-z]+)\s*", value)
    if not match:
        raise ValueError("Unsupported Docker memory value")
    number, unit = match.groups()
    return float(number) * {"B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3,
                           "kB": 1000, "MB": 1000**2, "GB": 1000**3}[unit]


class LoadTest:
    def __init__(self, args, key):
        self.args, self.key = args, key
        self.root = args.output
        self.root.mkdir(parents=True, exist_ok=False)
        self.stop = threading.Event()
        self.monitor_done = threading.Event()
        self.lock = threading.Lock()
        self.reason = None
        self.results = []
        self.samples = []
        self.posts = Counter()
        self.build = None
        self.start = time.monotonic()
        self.cpu_before = None
        self.run_id = uuid.uuid4().hex
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def write(self, name, value):
        with self.lock:
            with (self.root / name).open("a") as stream:
                stream.write(json.dumps(value, ensure_ascii=False) + "\n")

    def abort(self, reason):
        if not self.stop.is_set():
            self.reason = reason
            self.stop.set()
            self.write("events.jsonl", {"event": "stop_new_submissions", "reason": reason,
                                        "elapsed_s": round(time.monotonic() - self.start, 3)})
            print(json.dumps({"event": "stop_new_submissions", "reason": reason}), flush=True)

    def request(self, path, data=None, timeout=15, extra_headers=None):
        url = urllib.parse.urljoin(self.args.base_url + "/", path)
        original, resolved = urllib.parse.urlsplit(self.args.base_url), urllib.parse.urlsplit(url)
        if (original.scheme, original.netloc) != (resolved.scheme, resolved.netloc):
            raise ValueError("Cross-origin result URL")
        req = urllib.request.Request(url, data=data, headers={"Authorization": "Bearer " + self.key,
                                                            "Content-Type": "application/pdf",
                                                            **(extra_headers or {})})
        try:
            with self.opener.open(req, timeout=timeout) as response:
                return response.status, json.load(response), response.headers
        except urllib.error.HTTPError as exc:
            # Do not save raw response bodies, authentication headers or document content.
            status, headers = exc.code, exc.headers
            code = None
            try:
                code = json.loads(exc.read(4096)).get("error", {}).get("code")
            except (ValueError, AttributeError):
                pass
            exc.close()
            return status, {"error_code": code}, headers

    def run_one(self, spec):
        started = time.monotonic()
        row = {"id": spec["id"], "category": spec["category"], "source": spec["source"],
               "round": spec.get("round"), "tenant_id": getattr(self.args, "tenant_id", None),
               "filename": spec["filename"], "expected_pages": spec.get("pages"),
               "started_elapsed_s": round(started - self.start, 3), "attempts": 0,
               "http_429": 0, "status": "not_submitted"}
        try:
            source = self.args.fixtures / spec["source"]
            row["file_bytes"] = source.stat().st_size
            post_headers = {"Content-Length": str(row["file_bytes"])}
            idempotent = getattr(self.args, "idempotent_submissions", False)
            if idempotent:
                digest = hashlib.sha256()
                with source.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                post_headers.update({"Idempotency-Key": self.run_id + "-" + str(spec["id"]),
                                     "X-Content-SHA256": digest.hexdigest()})
            network_retries = 0
            result_url = None
            while not self.stop.is_set():
                if time.monotonic() - self.start > self.args.max_wall:
                    self.abort("load_test_wall_limit")
                    break
                row["attempts"] += 1
                try:
                    with source.open("rb") as body:
                        parameters = {"filename": spec["filename"], "tier": "basic", "output_formats": "markdown"}
                        if row["tenant_id"] is not None:
                            parameters["tenant_id"] = row["tenant_id"]
                        status, task, headers = self.request("/v1/tasks?" + urllib.parse.urlencode(parameters), body, 60, post_headers)
                except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
                    cause = getattr(exc, "reason", exc)
                    row.update(exception_type=type(exc).__name__, cause_type=type(cause).__name__,
                               network_errno=getattr(cause, "errno", None))
                    if not idempotent or network_retries >= 3:
                        raise
                    network_retries += 1
                    row["submit_network_retries"] = network_retries
                    self.stop.wait(min(5, network_retries))
                    continue
                with self.lock:
                    self.posts[str(status)] += 1
                if status == 202:
                    row.update(task_id=task["task_id"], status="accepted",
                               admission_wait_s=round(time.monotonic() - started, 3))
                    result_url = task["result_url"]
                    self.write("accepted.jsonl", row)
                    break
                if status == 429 or (idempotent and status == 409 and (task or {}).get("error_code") == "upload_in_progress"):
                    row["http_429"] += int(status == 429)
                    delay = max(1, min(15, float(headers.get("Retry-After", "5"))))
                    self.stop.wait(delay + random.uniform(0, 1))
                    continue
                row.update(status="submit_rejected", http_status=status)
                if status in {401, 403}:
                    self.abort("authentication_failed")
                break
            # Accepted work is allowed to finish after stopping new submissions.
            deadline = time.monotonic() + self.args.task_poll_timeout
            while result_url and time.monotonic() < deadline:
                status, result, _ = self.request(result_url)
                if status != 200:
                    row.update(status="poll_http_error", http_status=status)
                    break
                if result["build"] != self.build:
                    self.abort("service_build_changed")
                if result["status"] in {"completed", "failed"}:
                    row.update(status=result["status"], build=result["build"],
                               queue_s=result.get("queued_ms", 0) / 1000,
                               processing_s=result.get("duration_ms", 0) / 1000,
                               page_count=result.get("page_count"), processed_pages=result.get("processed_pages"),
                               model_calls=result.get("model_calls", 0), usage=result.get("usage", {}),
                               warnings=result.get("warnings", []),
                               stage=result.get("stage"), current_page=result.get("current_page"),
                               route_reasons=result.get("route_reasons", []),
                               routes=[p["parser"] for p in result.get("pages", [])],
                               error_code=(result.get("error") or {}).get("code"),
                               server_e2e_s=round(result["completed_at"] - result["created_at"], 3))
                    if row["status"] == "completed":
                        markdown = result["files"][0]["content"]["markdown"]
                        row["content_characters"] = len(markdown)
                        if not markdown.strip() or row["processed_pages"] != spec.get("pages", row["processed_pages"]):
                            row.update(status="result_validation_failed", error_code="empty_or_page_count_mismatch")
                    break
                time.sleep(1)
            else:
                if result_url:
                    row["status"] = "poll_timeout_unknown"
        except Exception as exc:
            # A failed POST may have been accepted: do not retry it and double-charge a model call.
            row.update(status="request_exception_unknown" if row["attempts"] else "fixture_error",
                       exception_type=type(exc).__name__)
        row["client_e2e_s"] = round(time.monotonic() - started, 3)
        row["batch_completion_s"] = round(time.monotonic() - self.start, 3)
        self.write("documents.jsonl", row)
        with self.lock:
            self.results.append(row)
            done = len(self.results)
        print(json.dumps({"event": "document_finished", "id": row["id"], "status": row["status"],
                          "finished": done, "elapsed_s": round(time.monotonic() - self.start, 1),
                          "processing_s": row.get("processing_s"), "http_429": row["http_429"]}), flush=True)
        return row

    def sample(self):
        mem = {line.split(":")[0]: int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines()
               if line.startswith(("MemTotal:", "MemAvailable:", "SwapTotal:", "SwapFree:"))}
        cpu = [int(v) for v in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
        cpu_percent = None
        if self.cpu_before:
            change = [a - b for a, b in zip(cpu, self.cpu_before)]
            cpu_percent = round(100 * (sum(change) - change[3] - change[4]) / max(1, sum(change)), 2)
        self.cpu_before = cpu
        health_started = time.monotonic()
        status, health, _ = self.request("/v1/health", timeout=5)
        health_rtt_ms = round((time.monotonic() - health_started) * 1000, 3)
        raw = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                             capture_output=True, text=True, timeout=12, check=True)
        containers = [json.loads(line) for line in raw.stdout.splitlines() if line]
        measured = next((c for c in containers if c["Name"] == "docbreaker"), None)
        inspect = json.loads(subprocess.run(["docker", "inspect", "--format",
                                  '{"oom":{{json .State.OOMKilled}},"restarts":{{json .RestartCount}},"memory_limit":{{json .HostConfig.Memory}},"nano_cpus":{{json .HostConfig.NanoCpus}}}', "docbreaker"],
                                 capture_output=True, text=True, timeout=5, check=True).stdout)
        return {"elapsed_s": round(time.monotonic() - self.start, 3), "unix_time": time.time(),
                "host_cpu_percent": cpu_percent, "load_average": list(os.getloadavg()), "memory_kib": mem,
                "disk_free_bytes": shutil.disk_usage(self.args.fixtures).free,
                "health_http_status": status, "health_rtt_ms": health_rtt_ms,
                "health": health, "containers": containers,
                "docbreaker_memory_bytes": memory_bytes(measured["MemUsage"].split(" / ")[0]) if measured else None,
                "harness_cpu_seconds": resource.getrusage(resource.RUSAGE_SELF).ru_utime + resource.getrusage(resource.RUSAGE_SELF).ru_stime,
                "harness_max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                "oom_killed": inspect["oom"], "restart_count": inspect["restarts"],
                "memory_limit": inspect["memory_limit"], "nano_cpus": inspect["nano_cpus"]}

    def monitor(self):
        unsafe = 0
        while not self.monitor_done.is_set():
            try:
                sample = self.sample()
                self.samples.append(sample)
                self.write("resources.jsonl", sample)
                if sample["health_http_status"] != 200 or sample["memory_kib"]["MemAvailable"] < 1024**2:
                    unsafe += 1
                else:
                    unsafe = 0
                if sample["oom_killed"] or sample["restart_count"] > self.initial_restarts:
                    self.abort("container_oom_or_restart")
                elif sample["disk_free_bytes"] < 2 * 1024**3:
                    self.abort("host_disk_below_2GiB")
                elif unsafe >= 3:
                    self.abort("unhealthy_or_memory_pressure")
                if sample["health"] and sample["health"]["build"] != self.build:
                    self.abort("service_build_changed")
                if sample["health"] and sample["health"].get("model_in_flight", 0) > sample["health"].get("model_concurrency", 50):
                    self.abort("model_concurrency_exceeded")
            except Exception as exc:
                self.write("events.jsonl", {"event": "monitor_error", "type": type(exc).__name__})
                unsafe += 1
                if unsafe >= 3:
                    self.abort("monitor_unavailable")
            self.monitor_done.wait(self.args.sample_interval)

    def run(self, specs):
        baseline = self.sample()
        self.samples.append(baseline)
        self.write("resources.jsonl", baseline)
        if baseline["health_http_status"] != 200 or not baseline["health"]["accepting"]:
            raise RuntimeError("Service is not healthy/accepting")
        if self.args.expect_build and baseline["health"]["build"] != self.args.expect_build:
            raise RuntimeError("Deploy the expected build before starting this benchmark")
        if self.args.expect_concurrency and baseline["health"]["concurrency"] != self.args.expect_concurrency:
            raise RuntimeError("Expected document concurrency is not deployed")
        if getattr(self.args, "expect_model_concurrency", None) and baseline["health"].get("model_concurrency") != self.args.expect_model_concurrency:
            raise RuntimeError("Expected global model concurrency is not deployed")
        if getattr(self.args, "expect_model", None) and baseline["health"].get("model") != self.args.expect_model:
            raise RuntimeError("Expected model is not deployed")
        if self.args.expect_unlimited and (baseline["memory_limit"] or baseline["nano_cpus"]):
            raise RuntimeError("CPU/memory restrictions have not been removed")
        if baseline["health"]["queued"] + baseline["health"]["running"] + baseline["health"]["uploading"]:
            raise RuntimeError("Service is busy before test; retry when idle")
        if baseline["memory_kib"]["MemAvailable"] < 1536 * 1024:
            raise RuntimeError("Need at least 1.5GiB host available memory")
        self.build = baseline["health"]["build"]
        self.initial_restarts = baseline["restart_count"]
        self.start = time.monotonic()
        manifest = {"build": self.build, "clients": self.args.clients, "documents": specs,
                    "model": baseline["health"].get("model"),
                    "model_concurrency": baseline["health"].get("model_concurrency"),
                    "sample_interval_s": self.args.sample_interval, "cpu_count": os.cpu_count(),
                    "note": "Client runs on target host, outside measured container; not a cross-host network benchmark"}
        (self.root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        sampler = threading.Thread(target=self.monitor, daemon=True)
        sampler.start()
        try:
            with ThreadPoolExecutor(max_workers=self.args.clients) as pool:
                futures = [pool.submit(self.run_one, spec) for spec in specs]
                for future in as_completed(futures):
                    future.result()
        finally:
            self.elapsed = time.monotonic() - self.start
            self.monitor_done.set()
            sampler.join(timeout=20)
        summary = {"build": self.build, "documents": len(specs), "finished": len(self.results),
                   "status_counts": dict(Counter(r["status"] for r in self.results)),
                   "http_post_counts": dict(self.posts), "wall_seconds": round(self.elapsed, 3),
                   "throughput_completed_docs_per_minute": round(60 * sum(r["status"] == "completed" for r in self.results) / self.elapsed, 3),
                   "stop_reason": self.reason, "categories": {}}
        for category in ["all"] + sorted({r["category"] for r in self.results}):
            selected = [r for r in self.results if category == "all" or r["category"] == category]
            successful = [r for r in selected if r["status"] == "completed"]
            summary["categories"][category] = {"status_counts": dict(Counter(r["status"] for r in selected)),
                "success_processing_s": distribution([r["processing_s"] for r in successful]),
                "success_queue_s": distribution([r["queue_s"] for r in successful]),
                "success_client_e2e_s": distribution([r["client_e2e_s"] for r in successful]),
                "success_server_e2e_s": distribution([r["server_e2e_s"] for r in successful])}
        summary["model_calls"] = sum(r.get("model_calls", 0) for r in self.results)
        summary["total_tokens"] = sum(r.get("usage", {}).get("total_tokens", 0) for r in self.results)
        summary["failure_codes"] = dict(Counter(r.get("error_code") or r["status"] for r in self.results if r["status"] != "completed"))
        (self.root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        print(json.dumps({"event": "test_finished", **summary}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--tenant-id", required=True, type=int, help="Tenant attribution for every test task")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--fixtures", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--clients", type=int, default=20)
    parser.add_argument("--sample-interval", type=float, default=5)
    parser.add_argument("--max-wall", type=int, default=3600)
    parser.add_argument("--task-poll-timeout", type=int, default=330)
    parser.add_argument("--expect-build")
    parser.add_argument("--expect-concurrency", type=int)
    parser.add_argument("--expect-model-concurrency", type=int)
    parser.add_argument("--expect-model")
    parser.add_argument("--expect-unlimited", action="store_true")
    parser.add_argument("--idempotent-submissions", action="store_true")
    args = parser.parse_args()
    if not 0 < args.tenant_id <= 2**63 - 1:
        parser.error("tenant-id must be a positive 64-bit integer")
    if not 1 <= args.clients <= 20:
        parser.error("clients must be 1..20")
    key = sys.stdin.read().strip()
    if not key:
        parser.error("Read the existing API key from stdin")
    specs = json.loads(args.manifest.read_text())
    if len(specs) != 100 or len({s["id"] for s in specs}) != 100:
        parser.error("Manifest must contain exactly 100 distinct logical document IDs")
    for spec in specs:
        source = (args.fixtures / spec["source"]).resolve()
        if args.fixtures.resolve() not in source.parents or not source.is_file():
            parser.error("Fixture must exist inside fixtures directory")
    test = LoadTest(args, key)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: test.abort("operator_signal"))
    test.run(specs)


if __name__ == "__main__":
    main()
