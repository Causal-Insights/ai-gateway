#!/usr/bin/env python3
"""Check or regenerate the provider reference's model inventory from live code/config."""

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml

from generation_job_adapters import ProviderAdapterError, route_for

START = "<!-- BEGIN GENERATED PROVIDER INVENTORY -->"
END = "<!-- END GENERATED PROVIDER INVENTORY -->"


def code(value):
    return "`" + str(value).replace("|", "&#124;").replace("`", "&#96;") + "`"


def render(config, registry, document):
    catalog = config["model_list"]
    aliases = [item["model_name"] for item in catalog]
    if len(aliases) != len(set(aliases)):
        raise ValueError("Duplicate configured aliases")
    if set(aliases) != set(registry["models"]):
        raise ValueError("Configuration and pricing alias sets differ")
    handlers = {
        item["provider"]: item["custom_handler"]
        for item in config["litellm_settings"]["custom_provider_map"]
    }
    rows, counts, upstreams = [], Counter(), set()
    for item in catalog:
        alias = item["model_name"]
        params = item["litellm_params"]
        model = registry["models"][alias]
        deployment = params["model"]
        upstream = model["upstream_model"]
        if model.get("deployment_model", upstream) != deployment:
            raise ValueError(f"{alias}: configured and pricing deployment identities differ")
        profiles = [registry["profiles"][key] for key in model["profiles"]]
        vendors = {profile["vendor"] for profile in profiles}
        if len(vendors) != 1:
            raise ValueError(f"{alias}: expected exactly one serving vendor, got {vendors}")
        vendor = vendors.pop()
        if f'<a id="provider-{vendor}"></a>' not in document:
            raise ValueError(f"{vendor}: add a provider implementation section first")
        procedure = registry["vendors"][vendor]["procedure"]
        if not (ROOT / procedure).is_file():
            raise ValueError(f"{vendor}: missing verification procedure: {procedure}")
        counts[vendor] += 1
        upstreams.add(upstream)
        enabled = [profile for profile in profiles if profile["enabled"]]
        admission = "disabled" if model.get("disabled") else "priced" if enabled else "blocked"
        prefix = deployment.split("/", 1)[0]
        dispatch = code(handlers[prefix]) if prefix in handlers else f"LiteLLM {code(prefix + '/')}"
        durable = []
        if any("generation_job" in profile["routes"] for profile in enabled):
            for version in (1, 2):
                try:
                    route = code(route_for(alias, version))
                except ProviderAdapterError as exc:
                    if exc.code != "UNSUPPORTED_MODEL" or version == 1:
                        raise
                    route = "unavailable"
                durable.append(f"V{version}: {route}")
        settings = [f"{key}: {code(params[key])}" for key in ("vertex_location", "reasoning_effort") if key in params]
        identity = code(upstream)
        if deployment != upstream:
            identity += f"<br>Deployment: {code(deployment)}"
        rows.append("| " + " | ".join([
            code(alias), f"[{vendor}](#provider-{vendor})", identity,
            dispatch, "<br>".join(durable) or "—", "<br>".join(settings) or "—", admission,
        ]) + " |")
    summary = "; ".join(f"{vendor}: {counts[vendor]}" for vendor in sorted(counts))
    return (
        f"{START}\n\n"
        f"**{len(catalog)} configured aliases; {len(upstreams)} distinct upstream identities; "
        f"{len(counts)} serving providers.** Alias counts: {summary}.\n\n"
        "Generated from `litellm_config.yaml`, `pricing/registry.json`, and the executable "
        "`generation_job_adapters.route_for` dispatcher. `priced` means at least one "
        "enabled pricing profile; it does not certify access or every request option. "
        "The LiteLLM dispatch column identifies configuration, including legacy handlers; "
        "durable admission and model-specific restrictions still apply.\n\n"
        "| Public alias | Serving vendor | Exact upstream identity | LiteLLM dispatch | Durable dispatch | Configured location / effort | Admission |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        + "\n".join(rows) + f"\n\n{END}"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="regenerate only the marked inventory section")
    args = parser.parse_args()
    path = ROOT / "docs/provider-implementation-reference.md"
    try:
        document = path.read_text()
        if document.count(START) != 1 or document.count(END) != 1 or document.index(END) < document.index(START):
            raise ValueError("Expected exactly one ordered pair of inventory markers")
        config = yaml.safe_load((ROOT / "litellm_config.yaml").read_text())
        registry = json.loads((ROOT / "pricing/registry.json").read_text())
        inventory = render(config, registry, document)
        before, remainder = document.split(START)
        _, after = remainder.split(END)
        expected = before + inventory + after
        if args.write:
            path.write_text(expected)
        elif expected != document:
            raise ValueError("Provider inventory is stale; run python scripts/provider_inventory.py --write")
    except (ValueError, KeyError, OSError, ProviderAdapterError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Provider inventory {'regenerated' if args.write else 'verified'}: {len(config['model_list'])} aliases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
