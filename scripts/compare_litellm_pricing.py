#!/usr/bin/env python3
"""Compare an explicitly captured LiteLLM map with verified registry token rates.

Read-only. No inference requests, rate imports, admission changes or deployments.
An absent exact key is not proof that LiteLLM's provider-specific resolver fails.
"""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIELDS = {"input_tokens": "input_cost_per_token", "output_tokens": "output_cost_per_token",
          "cached_tokens": "cache_read_input_token_cost", "cache_write_tokens": "cache_creation_input_token_cost"}


def compare(catalog, registry):
    result = []
    for alias, model in registry["models"].items():
        upstream = model["upstream_model"]
        keys = list(dict.fromkeys(k for k in (upstream, upstream.split("/", 1)[-1]) if k in catalog))
        row = {"alias": alias, "upstream_model": upstream, "exact_keys": keys, "comparisons": [],
               "status": "no_exact_key" if not keys else "manual_dimensions_required"}
        if keys:
            entry = catalog[keys[0]]
            row["pricing_fields"] = {key: value for key, value in entry.items() if "cost" in key}
            for version in model["profiles"]:
                profile = registry["profiles"][version]
                # Provider charges, multimodal units, tools and account discounts
                # need their own semantic comparison, not guessed field names.
                if profile["status"] != "verified" or profile["extractor"] not in {"openai_text", "openai_text_legacy"}:
                    continue
                tier = profile.get("options", {}).get("service_tier", ["default"])
                suffix = "_flex" if "flex" in tier else "_priority" if "priority" in tier or "fast" in tier else ""
                for component in profile["components"]:
                    base = FIELDS.get(component["usage"])
                    if not base:
                        continue
                    dimensions = [(base + suffix, component["rate"])]
                    if component.get("long_context"):
                        dimensions.append((base + "_above_272k_tokens" + suffix, component["long_context"]["rate"]))
                    for field, rate in dimensions:
                        expected = Decimal(rate) / Decimal(component["per"])
                        actual = entry.get(field)
                        status = "missing_field" if actual is None else "agreement" if Decimal(str(actual)) == expected else "mismatch"
                        row["comparisons"].append({"pricing_version": version, "field": field,
                            "litellm_rate_per_token": str(actual) if actual is not None else None,
                            "verified_rate_per_token": str(expected), "status": status})
            if row["comparisons"]:
                statuses = {item["status"] for item in row["comparisons"]}
                row["status"] = "mismatch" if "mismatch" in statuses else "incomplete" if "missing_field" in statuses else "compared_dimensions_agree"
        result.append(row)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("map_json", type=Path, help="Captured official LiteLLM live or bundled model cost map")
    parser.add_argument("--source", required=True, help="Source URL or pinned-image/version identifier")
    args = parser.parse_args()
    content = args.map_json.read_bytes()
    registry = json.loads((ROOT / "pricing/registry.json").read_text())
    report = {"captured_comparison_at": datetime.now(timezone.utc).isoformat(), "source": args.source,
              "map_sha256": hashlib.sha256(content).hexdigest(),
              "limitations": ["Comparison is not automatic price verification or production acceptance.",
                  "Exact-key lookup does not exhaust LiteLLM provider-specific alias resolution.",
                  "This capture does not establish which map a running production revision loaded."],
              "models": compare(json.loads(content), registry)}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
