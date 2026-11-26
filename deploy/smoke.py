"""Check real PDF submission and task polling, optionally with a supplied PDF fixture."""

import argparse
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request


def sample_pdf(pages=None) -> bytes:
    """Small valid native-text PDF, using standard fonts and correct xref offsets."""
    pages = pages or [["DocBreaker deployment check", "Battery capacity 100Ah.", "Voltage 51.2V."]]
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for lines in pages:
        page_id = len(objects) + 1
        content_id = page_id + 1
        kids.append(f"{page_id} 0 R")
        commands = ["BT /F1 12 Tf 50 760 Td 18 TL"]
        for line in lines:
            literal = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            commands.append(f"({literal}) Tj T*")
        text = ("\n".join(commands) + "\nET").encode("ascii")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>".encode())
        objects.append(b"<< /Length " + str(len(text)).encode() + b" >>\nstream\n" + text + b"\nendstream")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>".encode()
    result = b"%PDF-1.4\n"
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(result)
    result += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    result += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    return result + f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--tenant-id", type=int, help="Tenant attribution for model calls; no default")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--expect", action="append", default=[])
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    if args.tenant_id is not None and not 0 < args.tenant_id <= 2**63 - 1:
        parser.error("tenant-id must be a positive 64-bit integer")
    if args.pdf and args.tenant_id is None:
        parser.error("Supply --tenant-id for real-PDF model verification")
    base = args.base_url.rstrip("/")
    auth = {"Authorization": "Bearer " + os.environ["DOCBREAKER_API_KEY"],
            "Content-Type": "application/octet-stream"}

    def request(path, data=None):
        url = urllib.parse.urljoin(base, path)
        root, resolved = urllib.parse.urlsplit(base), urllib.parse.urlsplit(url)
        assert (root.scheme, root.netloc) == (resolved.scheme, resolved.netloc), "Cross-origin result URL"
        with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=auth), timeout=30) as response:
            return json.load(response)

    assert request("/v1/health")["status"] == "healthy"
    try:
        urllib.request.urlopen(base + "/v1/health", timeout=5)
        raise AssertionError("Health endpoint allowed missing authentication")
    except urllib.error.HTTPError as exc:
        assert exc.code == 401
    data = args.pdf.read_bytes() if args.pdf else sample_pdf()
    filename = args.pdf.name if args.pdf else "deployment-check.pdf"
    started = time.monotonic()
    parameters = {"filename": filename, "tier": "basic", "output_formats": "markdown"}
    if args.tenant_id is not None:
        parameters["tenant_id"] = args.tenant_id
    task = request("/v1/tasks?" + urllib.parse.urlencode(parameters), data)
    deadline = started + args.timeout
    while time.monotonic() < deadline:
        result = request(task["result_url"])
        if result["status"] == "completed":
            assert result.get("tenant_id") == args.tenant_id
            assert len(result["files"]) == 1
            markdown = result["files"][0]["content"]["markdown"]
            assert markdown.strip()
            assert not re.search(r"!\[[^\]]*\]\(\s*(?!https?://|data:)", markdown, re.I)
            expected = args.expect or ([] if args.pdf else ["DocBreaker", "51.2V", "100Ah"])
            for value in expected:
                assert value.casefold() in markdown.casefold(), f"Missing expected fact: {value}"
            if not args.pdf:
                assert result["model_calls"] == 0
            if args.output_dir:
                args.output_dir.mkdir(parents=True, exist_ok=True)
                (args.output_dir / "knowledge.md").write_text(markdown)
                (args.output_dir / "run.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
            print(json.dumps({"task_id": result["task_id"], "tenant_id": result.get("tenant_id"), "status": "completed", "pages": result["page_count"],
                              "model_calls": result["model_calls"], "usage": result["usage"],
                              "duration_seconds": round(time.monotonic() - started, 2),
                              "warnings": result.get("warnings", []), "build": result["build"]}))
            return
        if result["status"] == "failed":
            raise RuntimeError(json.dumps({"task_id": result["task_id"], "error": result.get("error")}))
        time.sleep(.5)
    raise TimeoutError("PDF smoke task exceeded the polling deadline")


if __name__ == "__main__":
    main()
