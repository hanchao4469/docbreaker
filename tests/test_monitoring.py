"""Dashboard authorization, scoped aggregates, resource samples and safe admission control."""
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from docbreaker.api import create_app
from docbreaker.config import Settings
from docbreaker.jobs import Jobs, write_json
from docbreaker.monitoring import Monitor, SystemSampler
from deploy.smoke import sample_pdf
from tests.test_service import KEY, AUTH


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.jobs=Jobs(Settings(api_key=KEY,data_dir=self.root,retention_seconds=86400))
        self.monitor=Monitor(self.jobs,cache_seconds=0)
        self.now=time.time()

    def tearDown(self):
        self.temp.cleanup()

    def seed(self,n,tenant,status,**fields):
        task_id="task_"+f"{n:032x}"
        folder=self.root/task_id;folder.mkdir(exist_ok=True)
        write_json(folder/"run.json",{"task_id":task_id,"filename":f"file-{n}.pdf","tenant_id":tenant,
                   "status":status,"created_at":self.now-200+n,"usage":{},**fields})
        return task_id

    def test_counts_tenants_tokens_throughput_and_errors_are_retention_scoped(self):
        self.seed(1,123,"completed",page_count=4,processed_pages=4,duration_ms=2000,queued_ms=300,
                  completed_at=self.now-10,model_calls=2,usage={"prompt_tokens":20,"completion_tokens":10,"total_tokens":30},
                  pages=[{"parser":"local"},{"parser":"llm"},{"parser":"hybrid"},{"parser":"blank"}],
                  model_stats={"observed_attempts":2,"success":1,"retries":1,"duration_ms_total":1000,"errors":{"model_network_error":1}})
        self.seed(2,123,"queued",queued_at=self.now-30)
        self.seed(3,456,"running",page_count=10,processed_pages=3)
        self.seed(4,456,"failed",completed_at=self.now-5,error={"code":"model_unavailable","message":"not displayed"})
        self.seed(5,None,"completed",completed_at=self.now-1000,page_count=1,processed_pages=1)
        s=self.monitor.snapshot()
        self.assertEqual(s["totals"]["total"],5)
        self.assertEqual(s["totals"]["success_rate"],2/3)
        self.assertEqual(s["model"]["usage"]["total_tokens"],30)
        self.assertEqual(s["model"]["retries"],1)
        self.assertEqual(s["model"]["average_attempt_ms"],500)
        self.assertEqual(s["performance"]["docs_per_minute"],.2)
        self.assertEqual(s["performance"]["pages_per_minute"],.8)
        self.assertGreaterEqual(s["performance"]["oldest_queue_seconds"],30)
        self.assertEqual(s["totals"]["local_pages"],1)
        self.assertEqual(s["totals"]["model_pages"],2)
        self.assertEqual(len(s["tenants"]),3)
        self.assertEqual(s["recent_errors"][0]["code"],"model_unavailable")
        self.assertEqual(s["scope"]["retention_seconds"],86400)
        self.assertEqual(sum(b["completed"] for b in s["timeline"]),2)
        self.assertEqual(sum(b["failed"] for b in s["timeline"]),1)

    def test_degraded_completion_is_counted_separately_from_full_extraction(self):
        self.seed(1,101,"completed",completed_at=self.now,page_count=2,processed_pages=2,
                  pages=[{"parser":"llm"},{"parser":"local_fallback"}],
                  degraded_pages=[{"page":2,"code":"invalid_model_output","missing_content":True}],
                  missing_content_pages=[2])
        totals=self.monitor.snapshot()["totals"]
        self.assertEqual(totals["completed"],1)
        self.assertEqual(totals["model_pages"],2)
        self.assertEqual(totals["degraded_pages"],1)
        self.assertEqual(totals["missing_content_pages"],1)

    def test_task_filters_pagination_and_no_internal_fields(self):
        self.seed(1,123,"completed",completed_at=self.now,source_sha256="private",idempotency_digest="private")
        self.seed(2,456,"failed",error={"code":"task_timeout","message":"private"})
        self.seed(3,None,"queued")
        rows=self.monitor.tasks(limit=1)
        self.assertEqual(rows["total"],3)
        self.assertEqual(rows["items"][0]["tenant_id"],None)
        self.assertEqual(self.monitor.tasks(tenant="123")["total"],1)
        self.assertEqual(self.monitor.tasks(tenant="unassigned")["total"],1)
        self.assertEqual(self.monitor.tasks(status="failed")["items"][0]["error_code"],"task_timeout")
        self.assertEqual(len(self.monitor.tasks(offset=3)["items"]),0)
        self.assertNotIn("private",json.dumps(self.monitor.tasks()))

    def test_atomic_updates_cleanup_and_corrupted_state(self):
        task_id=self.seed(1,123,"queued")
        broken=self.root/("task_"+"f"*32);broken.mkdir();(broken/"run.json").write_text("broken")
        self.assertEqual(self.monitor.snapshot()["scope"]["invalid_state_files"],1)
        self.jobs.update(task_id,status="completed",completed_at=self.now)
        self.assertEqual(self.monitor.snapshot()["totals"]["completed"],1)
        (self.root/task_id/"run.json").unlink()
        self.assertEqual(self.monitor.snapshot()["totals"]["total"],0)

    def test_deployment_ready_requires_admission_paused_and_all_work_drained(self):
        self.assertFalse(self.monitor.snapshot()["service"]["safe_to_deploy"])
        self.jobs.accepting=False;self.jobs.active=1
        self.assertFalse(self.monitor.snapshot()["service"]["safe_to_deploy"])
        self.jobs.active=0;self.jobs.current.add("a")
        self.assertFalse(self.monitor.snapshot()["service"]["safe_to_deploy"])
        self.jobs.current.clear();self.jobs.uploading=1
        self.assertFalse(self.monitor.snapshot()["service"]["safe_to_deploy"])
        self.jobs.uploading=0;self.jobs.queue.put_nowait("a")
        self.assertFalse(self.monitor.snapshot()["service"]["safe_to_deploy"])
        self.jobs.queue.get_nowait()
        self.assertTrue(self.monitor.snapshot()["service"]["safe_to_deploy"])

    def test_no_usage_is_not_invented_for_historical_tasks(self):
        self.seed(1,123,"completed")
        s=self.monitor.snapshot()
        self.assertEqual(s["model"]["observed_attempts"],0)
        self.assertEqual(s["model"]["usage"]["total_tokens"],0)
        self.assertIsNone(s["model"]["average_attempt_ms"])


class ResourceTests(unittest.TestCase):
    def test_cpu_deltas_cgroup_limits_and_memory_have_explicit_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);proc=root/"proc";cg=root/"cg";proc.mkdir();cg.mkdir();(proc/"self").mkdir()
            (proc/"stat").write_text("cpu 100 0 50 1000 0 0 0 0\n")
            (proc/"meminfo").write_text("MemTotal: 100000 kB\nMemAvailable: 40000 kB\n")
            (proc/"self/statm").write_text("100 8 0")
            (cg/"cpu.stat").write_text("usage_usec 1000000\n")
            (cg/"cpu.max").write_text("200000 100000")
            (cg/"memory.current").write_text(str(10*1024**2))
            (cg/"memory.max").write_text(str(32*1024**2))
            sampler=SystemSampler(proc,cg)
            with patch("docbreaker.monitoring.time.monotonic",return_value=10):a=sampler.sample(root)
            self.assertIsNone(a["host_cpu_percent"])
            (proc/"stat").write_text("cpu 180 0 50 1020 0 0 0 0\n")
            (cg/"cpu.stat").write_text("usage_usec 3000000\n")
            with patch("docbreaker.monitoring.time.monotonic",return_value=12):b=sampler.sample(root)
            self.assertEqual(b["host_cpu_percent"],80)
            self.assertEqual(b["container_cpu_percent"],100)
            self.assertEqual(b["container_memory_bytes"],10*1024**2)
            self.assertEqual(b["container_memory_limit_bytes"],32*1024**2)
            self.assertEqual(b["host_memory_available_bytes"],40000*1024)
            self.assertGreater(b["disk"]["free_bytes"],0)
            (cg/"memory.max").write_text("max")
            self.assertIsNone(sampler.sample(root)["container_memory_limit_bytes"])

    def test_missing_linux_resource_files_do_not_break_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            s=SystemSampler(root/"absent",root/"absent").sample(root)
            self.assertIsNone(s["host_cpu_percent"])
            self.assertIsNone(s["container_memory_bytes"])


class DashboardApiTests(unittest.TestCase):
    def test_static_shell_is_public_but_metrics_tasks_and_controls_require_auth(self):
        with tempfile.TemporaryDirectory() as tmp, TestClient(create_app(Settings(api_key=KEY,data_dir=Path(tmp)))) as client:
            shell=client.get("/dashboard")
            self.assertEqual(shell.status_code,200)
            self.assertIn("frame-ancestors 'none'",shell.headers["content-security-policy"])
            self.assertNotIn(KEY,shell.text)
            self.assertEqual(client.get("/dashboard/dashboard.js").status_code,200)
            self.assertEqual(client.get("/dashboard/dashboard.css").status_code,200)
            self.assertEqual(client.get("/dashboard/run.json").status_code,404)
            for route in ("metrics","tasks"):
                self.assertEqual(client.get("/v1/dashboard/"+route).status_code,401)
                response=client.get("/v1/dashboard/"+route,headers=AUTH)
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.headers["cache-control"],"no-store")
                self.assertNotIn(KEY,response.text)
            self.assertEqual(client.post("/v1/admin/drain").status_code,401)
            self.assertEqual(client.get("/v1/dashboard/tasks?limit=101",headers=AUTH).status_code,422)
            self.assertEqual(client.get("/v1/dashboard/tasks?status=bogus",headers=AUTH).status_code,422)
            self.assertEqual(client.get("/v1/dashboard/tasks?tenant=../",headers=AUTH).status_code,422)

    def test_pause_rejects_only_new_work_and_waits_for_existing_queue(self):
        release=threading.Event()
        async def parser(*args):
            import asyncio
            while not release.is_set():await asyncio.sleep(.005)
            return {"page_count":1,"processed_pages":1,"pages":[],"usage":{},"model_calls":0}
        with tempfile.TemporaryDirectory() as tmp, patch("docbreaker.jobs.parse_pdf",parser):
            app=create_app(Settings(api_key=KEY,data_dir=Path(tmp),concurrency=1))
            with TestClient(app) as client:
                for _ in range(2):self.assertEqual(client.post("/v1/tasks",content=sample_pdf(),headers=AUTH).status_code,202)
                self.assertEqual(client.post("/v1/admin/drain",headers=AUTH).status_code,200)
                metrics=client.get("/v1/dashboard/metrics",headers=AUTH).json()
                self.assertFalse(metrics["service"]["safe_to_deploy"])
                rejected=client.post("/v1/tasks",content=sample_pdf(),headers=AUTH)
                self.assertEqual(rejected.status_code,429)
                self.assertEqual(rejected.json()["error"]["code"],"service_draining")
                self.assertEqual(app.state.jobs.active,2)
                release.set()
                for _ in range(200):
                    metrics=client.get("/v1/dashboard/metrics",headers=AUTH).json()
                    if metrics["service"]["safe_to_deploy"]:break
                    time.sleep(.01)
                self.assertTrue(metrics["service"]["safe_to_deploy"])
                self.assertEqual(client.post("/v1/admin/resume",headers=AUTH).status_code,200)
                self.assertTrue(client.get("/v1/dashboard/metrics",headers=AUTH).json()["service"]["accepting"])


if __name__=="__main__":unittest.main()
