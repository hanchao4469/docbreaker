"""Authenticated readiness check without command-line secrets."""
import json
import os
import urllib.request

request = urllib.request.Request("http://127.0.0.1:8000/v1/health",
                                 headers={"Authorization": "Bearer " + os.environ["DOCBREAKER_API_KEY"]})
with urllib.request.urlopen(request, timeout=3) as response:
    assert json.load(response)["status"] == "healthy"
