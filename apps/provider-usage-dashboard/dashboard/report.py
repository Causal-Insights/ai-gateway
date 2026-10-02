from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from .internal import model_catalog

from .model import PROVIDERS, SOURCES, days, decimal, total


def summarize(rows):
    currencies = sorted({r["currency"] for r in rows if r.get("currency") and r.get("cost") is not None})
    metrics = sorted({key for r in rows for key in r.get("metrics", {})})
    adjustment_keys = sorted({key for r in rows for key in r.get("adjustments", {})})
    # Reported costs win when both exist; estimates never enter reported totals or comparisons.
    estimated_rows = [r for r in rows if r.get("cost") is None and r.get("estimated_cost") is not None]
    estimated_currencies = sorted({r["currency"] for r in estimated_rows if r.get("currency")})
    spending_currencies = sorted(set(currencies) | set(estimated_currencies))
    return {"cost": {currency: total(r["cost"] for r in rows if r.get("currency") == currency) for currency in currencies},
            "estimated_cost": {c: total(r["estimated_cost"] for r in estimated_rows if r.get("currency") == c) for c in estimated_currencies},
            "spending": {c: total(r.get("cost") if r.get("cost") is not None else r.get("estimated_cost")
                                  for r in rows if r.get("currency") == c) for c in spending_currencies},
            "estimate_partial": any(r.get("estimate_partial", False) for r in estimated_rows),
            "unpriced_tasks": sum(r.get("unpriced_tasks", 0) for r in rows),
            "metrics": {key: total(r.get("metrics", {}).get(key) for r in rows) for key in metrics},
            "adjustments": {key: {currency: total(r.get("adjustments", {}).get(key) for r in rows if r.get("currency") == currency)
                                  for currency in sorted({r["currency"] for r in rows if r.get("currency") and key in r.get("adjustments", {})})}
                            for key in adjustment_keys},
            "pending": sum(r.get("pending", 0) for r in rows)}


def source_status(statuses, now):
    if not statuses:
        return {"state": "not_configured", "message": "Not collected yet", "last_success": None}
    successes = [s["last_success"] for s in statuses if s.get("last_success")]
    errors = [s.get("message", "Reporting unavailable") for s in statuses if s.get("state") != "updated"]
    state = "partial" if errors and successes else "not_configured" if all(s.get("state") == "not_configured" for s in statuses) else "error" if errors else "updated"
    last = min(successes, default=None)
    if last and datetime.fromisoformat(last) < now - timedelta(hours=2):
        state = "stale"
    return {"state": state, "last_success": last, "message": "; ".join(dict.fromkeys(errors)),
            "earliest": min((s["earliest"] for s in statuses if s.get("earliest")), default=None),
            "latest": max((s["latest"] for s in statuses if s.get("latest")), default=None)}


def compare(left, right, comparable, reason):
    a, b = decimal(left), decimal(right)
    if a is None or b is None:
        return {"absolute": None, "percent": None, "comparable": False, "reason": "Missing reported cost"}
    delta = a - b
    return {"absolute": str(delta), "percent": str(delta / b * 100) if b else None,
            "comparable": comparable, "reason": reason}


def build_report(data, start, end, providers=None, now=None):
    now = now or datetime.now(timezone.utc)
    today = now.date()
    providers = providers or list(PROVIDERS)
    all_snapshots = [s for s in data["days"] if s["provider"] in providers]
    selected = [s for s in all_snapshots if str(start) <= s["day"] <= str(end)]
    actual = [s for s in selected if s["source"] == "provider"]
    provider_rows, model_rows, comparison_rows = [], [], []
    catalog = model_catalog()
    for provider in providers:
        snapshots = [s for s in actual if s["provider"] == provider]
        rows = [r for s in snapshots for r in s["rows"]]
        today_rows = [r for s in all_snapshots if s["provider"] == provider and s["source"] == "provider" and s["day"] == str(now.astimezone(ZoneInfo(s["time_zone"])).date()) for r in s["rows"]]
        statuses = [s for k, s in data["status"].items() if k.startswith(f"provider:{provider}:")]
        coverage = {key: len({s["day"] for s in snapshots if key in s["covered"]})
                    for key in ("cost", "estimated_cost", "requests", "generations", "usage", "payments")}
        paid = [p for p in data["payments"] if p["provider"] == provider and str(start) <= p["day"] <= str(end) and p.get("kind") in ("topup", "payment")]
        provider_rows.append({"id": provider, "name": PROVIDERS[provider], **summarize(rows),
            "today": summarize(today_rows), "balance": data["balances"].get(provider),
            "payments": {c: total(p["amount"] for p in paid if p["currency"] == c) for c in sorted({p["currency"] for p in paid})},
            "coverage": coverage, "days_requested": (end - start).days + 1,
            "time_zones": sorted({s["time_zone"] for s in snapshots}),
            "basis": sorted({s["basis"] for s in snapshots if "cost" in s["covered"] or "estimated_cost" in s["covered"]}),
            "services": [{"name": service, **summarize([r for r in rows if r.get("billing_service", r.get("service")) == service])}
                         for service in sorted({r.get("billing_service", r.get("service")) for r in rows if r.get("service")})],
            "notes": sorted({s["message"] for s in statuses if s.get("state") == "updated" and s.get("message")}),
            "status": source_status(statuses, now)})
        grouped = defaultdict(list)
        for r in rows:
            grouped[(r.get("model"), None if r.get("model") else r.get("service"))].append(r)
        for (model, service), items in grouped.items():
            model_rows.append({"provider": provider, "provider_name": PROVIDERS[provider],
                "model": model or "Unassigned", "service": service,
                "aliases": sorted({v["alias"] for v in catalog.values() if v["provider"] == provider and model and v["upstream_model"].split("/", 1)[-1] == model and v["alias"] != model}),
                **summarize(items)})
        source_totals, scopes, complete = {}, {}, {}
        for source in SOURCES:
            ss = [s for s in selected if s["provider"] == provider and s["source"] == source]
            rr = [r for s in ss for r in s["rows"]]
            source_totals[source] = summarize(rr)
            cost_ss = [s for s in ss if "cost" in s["covered"]]
            scopes[source] = sorted({s["scope"] for s in cost_ss})
            complete[source] = len({s["day"] for s in cost_ss}) == (end - start).days + 1 and not source_totals[source]["pending"]
        differences = {}
        for left, right in (("provider", "gateway"), ("gateway", "litellm"), ("gateway", "magiclens")):
            matched = bool(scopes[left]) and scopes[left] == scopes[right]
            comparable = matched and complete[left] and complete[right]
            reason = "Same reporting scope" if comparable else "Different reporting scope" if not matched else "Partial reporting period"
            differences[f"{left}_{right}"] = compare(source_totals[left]["cost"].get("USD"), source_totals[right]["cost"].get("USD"), comparable, reason)
        comparison_rows.append({"provider": provider, "name": PROVIDERS[provider], "sources": source_totals,
                                "scopes": scopes, "differences": differences})
    chart = []
    for day in days(start, end):
        ss = [s for s in actual if s["day"] == str(day)]
        rr = [r for s in ss for r in s["rows"]]
        daily_providers, daily_models = {}, {}
        for provider in providers:
            provider_items = [r for s in ss if s["provider"] == provider for r in s["rows"]]
            daily_providers[provider] = summarize(provider_items)
            daily_models[provider] = {model: summarize([r for r in provider_items if r.get("model") == model])
                                      for model in {r["model"] for r in provider_items if r.get("model")}}
        chart.append({"day": str(day), **summarize(rr),
            "models": {p: {m: v["cost"].get("USD") for m, v in mm.items()} for p, mm in daily_models.items()},
            "spending_models": {p: {m: v["spending"].get("USD") for m, v in mm.items()} for p, mm in daily_models.items()},
            "providers": {p: v["cost"].get("USD") for p, v in daily_providers.items()},
            "spending_providers": {p: v["spending"].get("USD") for p, v in daily_providers.items()}})
    warnings = activity_warnings(all_snapshots, providers, now)
    for source in SOURCES[1:]:
        status = source_status([s for k, s in data["status"].items() if k.split(":")[0] == source and k.split(":")[1] in providers], now)
        if status["state"] in ("error", "partial", "stale"):
            warnings.append({"provider": None, "kind": "reporting", "message": f'{source}: {status["message"] or "Reporting is stale"}'})
    for p in provider_rows:
        if p["status"]["state"] in ("error", "partial", "stale"):
            warnings.append({"provider": p["id"], "kind": "reporting", "message": f'{p["name"]}: {p["status"]["message"] or "Reporting is stale"}'})
    # Only compare windows old enough for billing systems to catch up.
    if end <= today - timedelta(days=2):
        for comp in comparison_rows:
            for pair, difference in comp["differences"].items():
                amount, percent = decimal(difference["absolute"]), decimal(difference["percent"])
                if difference["comparable"] and amount is not None and abs(amount) > 5 and (percent is None or abs(percent) > 10):
                    warnings.append({"provider": comp["provider"], "kind": "difference", "message": f'{comp["name"]}: {pair.replace("_", " / ")} differs by ${abs(amount):.2f}.'})
    main_rows = [r for s in actual for r in s["rows"]]
    today_all = [r for s in all_snapshots if s["source"] == "provider" and s["day"] == str(now.astimezone(ZoneInfo(s["time_zone"])).date()) for r in s["rows"]]
    payment_events = [p for p in data["payments"] if p["provider"] in providers and str(start) <= p["day"] <= str(end)]
    pay = [p for p in payment_events if p["kind"] in ("topup", "payment")]
    return {"start": str(start), "end": str(end), "today": str(today), "summary": summarize(main_rows),
            "today_summary": summarize(today_all), "payments_summary": {c: total(p["amount"] for p in pay if p["currency"] == c) for c in sorted({p["currency"] for p in pay})},
            "providers": provider_rows, "models": model_rows, "daily": chart, "comparisons": comparison_rows,
            "payments": sorted(payment_events, key=lambda p: p["day"], reverse=True), "warnings": warnings,
            "source_status": data["status"], "generated_at": now.isoformat()}


def activity_warnings(snapshots, providers, now):
    warnings = []
    for provider in providers:
        ss = [s for s in snapshots if s["source"] == "provider" and s["provider"] == provider]
        zone = "Asia/Shanghai" if provider == "byteplus" else "UTC"
        yesterday = now.astimezone(ZoneInfo(zone)).date() - timedelta(days=1)
        daily = {str(d): summarize([r for s in ss if s["day"] == str(d) for r in s["rows"]])
                 for d in days(yesterday - timedelta(days=7), yesterday)}
        for metric, multiplier, increase in (("cost", 2, 10), ("requests", 3, 100), ("generations", 3, 100)):
            def value(day):
                bucket = daily[str(day)]
                return decimal(bucket["cost"].get("USD") if metric == "cost" else bucket["metrics"].get(metric))
            current = value(yesterday)
            previous = [value(d) for d in days(yesterday - timedelta(days=7), yesterday - timedelta(days=1))]
            if current is not None and all(v is not None for v in previous):
                baseline = sum(previous) / 7
                if current > baseline * multiplier and current - baseline >= increase:
                    warnings.append({"provider": provider, "kind": "increase", "message": f'{PROVIDERS[provider]}: yesterday’s {metric} was above its seven-day baseline.'})
        history_start = str(yesterday - timedelta(days=30))
        historic = [s for s in ss if history_start <= s["day"] < str(yesterday) and s["covered"]]
        if len({s["day"] for s in historic}) == 30:
            old_models = {r["model"] for s in historic for r in s["rows"] if r.get("model")}
            new_models = {r["model"] for s in ss if s["day"] == str(yesterday) for r in s["rows"] if r.get("model") and any(decimal(v) and decimal(v) > 0 for v in [r.get("cost"), *r.get("metrics", {}).values()])}
            for model in sorted(new_models - old_models):
                warnings.append({"provider": provider, "kind": "new_model", "message": f'{PROVIDERS[provider]}: new activity for {model}.'})
    return warnings
