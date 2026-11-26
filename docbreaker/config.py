"""Validated runtime configuration; secrets are never included in diagnostics."""

import os
import math
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Settings:
    api_key: str = field(repr=False)
    data_dir: Path = Path("data")
    litellm_base_url: str = ""
    litellm_api_key: str = field(default="", repr=False)
    litellm_web_api_key: str = field(default="", repr=False)
    model: str = "gpt-6-luna"
    service_id: str = "pdf-parse"
    concurrency: int = 10
    model_concurrency: int = 50
    page_concurrency: int = 10
    render_concurrency: int = 2
    upload_concurrency: int = 10
    queue_size: int = 100
    max_upload_bytes: int = 50 * 1024 * 1024
    max_pages: int = 200
    task_timeout: float = 3600
    queue_timeout: float = 7200
    page_budget: float = 90
    min_task_timeout: float = 300
    command_timeout: float = 30
    model_timeout: float = 90
    model_retries: int = 1
    model_output_retries: int = 2
    max_completion_tokens: int = 12000
    render_dpi: int = 160
    render_max_pixels: int = 3200
    retention_seconds: int = 86400
    max_disk_bytes: int = 4 * 1024**3
    build: str = "development"

    def __post_init__(self):
        if len(self.api_key) < 32 or any(c.isspace() for c in self.api_key):
            raise ValueError("DOCBREAKER_API_KEY must have at least 32 non-whitespace characters")
        if self.litellm_base_url:
            url = urlsplit(self.litellm_base_url)
            if (url.scheme not in {"http", "https"} or not url.hostname or url.username
                    or url.password or url.query or url.fragment or url.path not in {"", "/"}):
                raise ValueError("LITELLM_BASE_URL must be a root HTTP(S) URL")
        if bool(self.litellm_api_key) != bool(self.litellm_base_url):
            raise ValueError("Configure both LiteLLM URL and key, or neither for local-only operation")
        if self.service_id != "pdf-parse":
            raise ValueError("Service ID must be pdf-parse")
        for name in ("concurrency", "model_concurrency", "page_concurrency", "render_concurrency", "upload_concurrency",
                     "queue_size", "max_upload_bytes", "max_pages", "task_timeout", "queue_timeout",
                     "page_budget", "min_task_timeout", "command_timeout",
                     "model_timeout", "max_completion_tokens", "render_dpi", "render_max_pixels",
                     "retention_seconds", "max_disk_bytes"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.concurrency > 20 or max(self.page_concurrency, self.upload_concurrency) > 10:
            raise ValueError("Document concurrency must not exceed 20; page and upload concurrency must not exceed 10")
        if self.model_concurrency > 100:
            raise ValueError("Global model concurrency must not exceed 100")
        if not 0 <= self.model_output_retries <= 2:
            raise ValueError("MODEL_OUTPUT_RETRIES must be 0, 1 or 2")
        if not 0 <= self.model_retries <= 2:
            raise ValueError("MODEL_RETRIES must be 0, 1 or 2")

    @classmethod
    def from_env(cls):
        """Load the explicit deployment environment without logging values."""
        return cls(
            api_key=os.environ.get("DOCBREAKER_API_KEY", ""),
            data_dir=Path(os.environ.get("DOCBREAKER_DATA_DIR", "/data")),
            litellm_base_url=os.environ.get("LITELLM_BASE_URL", "").rstrip("/"),
            litellm_api_key=os.environ.get("LITELLM_API_KEY", ""),
            litellm_web_api_key=os.environ.get("LITELLM_WEB_API_KEY", ""),
            model=os.environ.get("LITELLM_MODEL", "gpt-6-luna"),
            concurrency=int(os.environ.get("CONCURRENCY", "10")),
            model_concurrency=int(os.environ.get("MODEL_CONCURRENCY", "50")),
            page_concurrency=int(os.environ.get("PAGE_CONCURRENCY", "10")),
            render_concurrency=int(os.environ.get("RENDER_CONCURRENCY", "2")),
            upload_concurrency=int(os.environ.get("UPLOAD_CONCURRENCY", "10")),
            queue_size=int(os.environ.get("QUEUE_SIZE", "100")),
            max_upload_bytes=int(os.environ.get("MAX_UPLOAD_MB", "50")) * 1024**2,
            max_pages=int(os.environ.get("MAX_PAGES", "200")),
            task_timeout=float(os.environ.get("TASK_TIMEOUT_SECONDS", "3600")),
            queue_timeout=float(os.environ.get("QUEUE_TIMEOUT_SECONDS", "7200")),
            page_budget=float(os.environ.get("PAGE_BUDGET_SECONDS", "90")),
            min_task_timeout=float(os.environ.get("MIN_TASK_TIMEOUT_SECONDS", "300")),
            command_timeout=float(os.environ.get("COMMAND_TIMEOUT_SECONDS", "30")),
            model_timeout=float(os.environ.get("MODEL_TIMEOUT_SECONDS", "90")),
            model_retries=int(os.environ.get("MODEL_RETRIES", "1")),
            model_output_retries=int(os.environ.get("MODEL_OUTPUT_RETRIES", "2")),
            max_completion_tokens=int(os.environ.get("MODEL_MAX_COMPLETION_TOKENS", "12000")),
            render_dpi=int(os.environ.get("RENDER_DPI", "160")),
            render_max_pixels=int(os.environ.get("RENDER_MAX_PIXELS", "3200")),
            retention_seconds=int(os.environ.get("RETENTION_SECONDS", "86400")),
            max_disk_bytes=int(os.environ.get("MAX_DATA_MB", "4096")) * 1024**2,
            build=os.environ.get("DOCBREAKER_BUILD", "development"),
        )
