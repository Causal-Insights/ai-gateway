#!/usr/bin/env python3
"""Validate evidence shape and vendor source boundaries, not source truth."""
import json
from pathlib import Path
from urllib.parse import urlsplit
import sys

ROOT = Path(__file__).resolve().parents[1]
HOSTS = {"openai": {"developers.openai.com", "platform.openai.com"},
         "google": {"cloud.google.com", "docs.cloud.google.com", "ai.google.dev"},
         "xai": {"docs.x.ai", "console.x.ai"}, "byteplus": {"docs.byteplus.com", "console.byteplus.com"},
         "elevenlabs": {"elevenlabs.io", "join.elevenlabs.io"}}


def validate(report):
    import jsonschema
    schema = json.loads((ROOT / "skills/price-verification/references/evidence.schema.json").read_text())
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(report)
    vendor = report["identity"]["vendor"]
    for source in report["sources"]:
        url = urlsplit(source["url"])
        if url.scheme != "https" or url.hostname not in HOSTS.get(vendor, set()):
            raise ValueError("Evidence source does not belong to the named vendor")
        if vendor == "google" and "Vertex" in report["identity"]["api_product"] and url.hostname == "ai.google.dev":
            raise ValueError("Gemini Developer API pricing cannot verify Vertex pricing")
        if vendor == "elevenlabs" and source["scope"] == "pricing" and url.path.rstrip("/") == "/pricing":
            raise ValueError("Consumer pricing cannot verify ElevenAPI pricing")
    if report["status"] == "verified" and not report["sources"]:
        raise ValueError("Verified evidence needs an opened official source")


def main():
    count = 0
    for path in sorted((ROOT / "pricing/evidence").glob("*.json")):
        report = json.loads(path.read_text())
        if isinstance(report, dict) and "schema_version" in report:
            validate(report)
            count += 1
    print(f"Validated {count} structured price reports; source applicability still requires human/agent review")


if __name__ == "__main__":
    main()
