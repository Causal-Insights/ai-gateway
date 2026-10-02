"""Official reporting APIs only; none of these adapters can submit inference or payments."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlencode

import httpx

from .model import Batch, NotConfigured, ReportingError, days, decimal, identifier, row


def require(env, name):
    value = env.get(name, "").strip()
    if not value:
        raise NotConfigured(f"{name} is not configured.")
    return value


def epoch(day):
    return int(datetime.combine(day, time(), timezone.utc).timestamp())


def iso_day(value, milliseconds=False):
    if isinstance(value, (int, float, Decimal)) or str(value).isdigit():
        return datetime.fromtimestamp(float(value) / (1000 if milliseconds else 1), timezone.utc).date().isoformat()
    return date.fromisoformat(str(value)[:10]).isoformat()


async def request(client, method, url, **kwargs):
    for attempt in range(3):
        response = await client.request(method, url, **kwargs)
        if response.status_code == 429 or response.status_code >= 500:
            if attempt < 2:
                delay = decimal(response.headers.get("retry-after"))
                await asyncio.sleep(min(float(delay or 2 ** attempt), 5))
                continue
        if response.is_error:
            raise ReportingError(f"Reporting API returned HTTP {response.status_code}. Check reporting access; previous data retained.")
        return json.loads(response.text, parse_float=Decimal)


async def openai(client, env, start, end, category):
    key = require(env, "OPENAI_USAGE_API_KEY")
    costs = category == "costs"
    batch = Batch("openai", category, start, end, scope=env.get("OPENAI_REPORTING_SCOPE", "OpenAI organization"))
    usage_endpoint = {"web_searches": "web_search_calls", "file_searches": "file_search_calls"}.get(category, category)
    endpoint = "costs" if costs else f"usage/{usage_endpoint}"
    group = ["line_item", "project_id"] if costs else ["model", "project_id"] if category not in ("vector_stores", "code_interpreter_sessions", "file_searches") else ["project_id"]
    params = [("start_time", epoch(start)), ("end_time", epoch(end + timedelta(days=1))),
              ("bucket_width", "1d"), ("limit", 31)] + [("group_by", g) for g in group]
    if env.get("OPENAI_PROJECT_IDS"):
        params += [("project_ids", v.strip()) for v in env["OPENAI_PROJECT_IDS"].split(",") if v.strip()]
    cursor, seen = None, set()
    while True:
        result = await request(client, "GET", f"https://api.openai.com/v1/organization/{endpoint}",
                               headers={"Authorization": f"Bearer {key}"}, params=params + ([("page", cursor)] if cursor else []))
        for bucket in result["data"]:
            day = iso_day(bucket["start_time"])
            batch.covered[day] = ["cost"] if costs else ["usage"]
            if costs and not bucket["results"]:
                batch.rows.append(row(day, cost="0"))
            for item in bucket["results"]:
                if costs:
                    amount = item.get("amount") or {}
                    # Billing names can differ from versioned usage model IDs.
                    # Preserve the reported name, including image/audio/text lines;
                    # do not map their costs onto a guessed version or allocate totals.
                    model_line = re.fullmatch(r"([^, ]+)(?: (?:image|audio|text))?, (?:input_tokens|output_tokens|input_cached_tokens|input_cache_write_tokens|(?:cached )?(?:input|output))", item.get("line_item") or "")
                    batch.rows.append(row(day, model=model_line[1] if model_line else None, cost=amount.get("value"), currency=amount.get("currency"),
                                          service=item.get("line_item") or "Unassigned", scope=item.get("project_id") or batch.scope))
                else:
                    metric_map = {"num_model_requests": "requests", "input_tokens": "input_tokens", "output_tokens": "output_tokens",
                                  "input_cached_tokens": "cached_input_tokens", "images": "images", "characters": "characters",
                                  "seconds": "audio_seconds", "usage_bytes": "storage_bytes", "num_sessions": "sessions"}
                    # Tool invocations are not additional model requests.
                    if category == "web_searches":
                        metric_map.pop("num_model_requests")
                        metric_map["num_requests"] = "web_search_calls"
                    if category == "file_searches":
                        metric_map["num_requests"] = "file_search_calls"
                    metrics = {dst: item[src] for src, dst in metric_map.items() if src in item}
                    batch.covered[day] += [m for m in ("requests", "generations") if m in metrics]
                    batch.rows.append(row(day, model=item.get("model"), service=category, metrics=metrics, scope=item.get("project_id") or batch.scope))
        if not result.get("has_more"):
            break
        cursor = result.get("next_page")
        if not cursor or cursor in seen:
            raise ReportingError("OpenAI returned incomplete pagination; previous data retained.")
        seen.add(cursor)
    return batch


async def xai_team(client, env, key):
    if env.get("XAI_TEAM_ID"):
        return env["XAI_TEAM_ID"]
    data = await request(client, "GET", "https://management-api.x.ai/auth/management-keys/validation",
                         headers={"Authorization": f"Bearer {key}"})
    team = data.get("scopeId") if data.get("scope") == "SCOPE_TEAM" else data.get("teamId")
    if not team:
        raise NotConfigured("Set XAI_TEAM_ID to the team associated with the reporting key.")
    return team


async def xai(client, env, start, end, category):
    key = require(env, "XAI_USAGE_API_KEY")
    team = await xai_team(client, env, key)
    headers = {"Authorization": f"Bearer {key}"}
    base = f"https://management-api.x.ai/v1/billing/teams/{team}"
    batch = Batch("xai", category, start, end, scope=env.get("XAI_REPORTING_SCOPE", "xAI team"))
    if category == "costs":
        result = await request(client, "POST", base + "/usage", headers=headers, json={"analyticsRequest": {
            "timeRange": {"startTime": f"{start} 00:00:00", "endTime": f"{end} 23:59:59", "timezone": "Etc/GMT"},
            "timeUnit": "TIME_UNIT_DAY", "values": [{"name": "usd", "aggregation": "AGGREGATION_SUM"}],
            "groupBy": ["description"], "filters": []}})
        if result.get("limitReached"):
            if start == end:
                raise ReportingError("xAI truncated its daily report; previous data retained.")
            midpoint = start + (end - start) // 2
            for a, b in ((start, midpoint), (midpoint + timedelta(days=1), end)):
                part = await xai(client, {**env, "XAI_TEAM_ID": team}, a, b, category)
                batch.rows.extend(part.rows); batch.covered.update(part.covered)
            return batch
        for series in result["timeSeries"]:
            description = (series.get("groupLabels") or series.get("group") or [None])[0]
            match = re.fullmatch(r"(?:API |Chat |Image |Video )?(grok[^, ]+)", description or "")
            for point in series["dataPoints"]:
                day = iso_day(point["timestamp"])
                batch.rows.append(row(day, model=match[1] if match else None, service=description,
                                      cost=point["values"][0]))
                batch.covered[day] = ["cost"]
    else:
        result = await request(client, "GET", base + "/prepaid/balance", headers=headers)
        amount = decimal((result.get("total") or {}).get("val"))
        # The API represents prepaid funds as a negative outstanding amount.
        batch.balance = {"amount": str(-amount / 100) if amount is not None else None, "unit": "USD", "label": "Prepaid balance"}
        batch.payments = []
        for change in result["changes"]:
            amount = decimal((change.get("amount") or {}).get("val"))
            timestamp = change.get("createTime") or change.get("createTs")
            if amount is None or not timestamp:
                continue
            origin = change.get("changeOrigin")
            # Spending is already represented by daily costs, not funding activity.
            if origin == "SPEND":
                continue
            purchase = origin in ("PURCHASE", "AUTO_PURCHASE")
            if purchase and change.get("topupStatus") != "SUCCEEDED":
                continue
            event = {"id": identifier(change.get("invoiceId"), timestamp, change.get("changeOrigin"), str(amount)),
                     "day": iso_day(timestamp), "kind": "topup" if purchase else "adjustment",
                     "amount": str(-amount / 100), "currency": "USD", "description": change.get("changeOrigin", "Balance adjustment")}
            batch.payments.append(event)
        # The endpoint lists returned events but doesn't guarantee a historical
        # coverage window. Empty history is unavailable, not an invented $0.
        for payment in batch.payments:
            batch.covered.setdefault(payment["day"], []).append("payments")
    return batch


async def elevenlabs(client, env, start, end, category):
    headers = {"xi-api-key": require(env, "ELEVENLABS_USAGE_API_KEY")}
    batch = Batch("elevenlabs", category, start, end, scope="ElevenLabs workspace")
    if category == "subscription":
        result = await request(client, "GET", "https://api.elevenlabs.io/v1/user/subscription", headers=headers)
        used, limit = decimal(result.get("character_count")), decimal(result.get("character_limit"))
        batch.balance = {"amount": str(limit - used) if used is not None and limit is not None else None,
            "unit": "credits", "label": "Remaining subscription allowance", "used": str(used) if used is not None else None,
            "limit": str(limit) if limit is not None else None, "reset_at": result.get("next_character_count_reset_unix"),
            "overage": result.get("current_overage")}
        # Decimal values from monetary overage must remain strings in the cache.
        if batch.balance["overage"]:
            batch.balance["overage"] = {k: str(v) for k, v in batch.balance["overage"].items()}
        return batch
    result = await request(client, "POST", "https://api.elevenlabs.io/v1/workspace/analytics/query/usage-by-product-over-time",
        headers=headers, json={"start_time": epoch(start) * 1000, "end_time": epoch(end + timedelta(days=1)) * 1000,
        "interval_seconds": 86400, "group_by": ["model", "product_type"], "time_zone": "UTC"})
    columns, units = result["columns"], result["column_units"]
    types = result.get("column_types", [None] * len(columns))
    time_columns = [i for i, unit in enumerate(units) if "time" in columns[i].lower() and (unit == "ms" or types[i] == "DateTime")]
    if not time_columns:
        raise ReportingError("ElevenLabs returned an unrecognized time column; previous data retained.")
    for values in result["rows"]:
        item = dict(zip(columns, values, strict=True))
        day = iso_day(values[time_columns[0]], milliseconds=units[time_columns[0]] == "ms")
        # Analytics can include a bucket exactly at the requested end boundary.
        if not str(start) <= day <= str(end):
            continue
        metrics = {}
        cost, currency = None, None
        for name, unit, value in zip(columns, units, values, strict=True):
            if decimal(value) is None or name in (columns[time_columns[0]], "model", "product_type"):
                continue
            if unit in ("usd", "eur", "gbp", "inr", "pln"):
                if name == "total_cost":
                    cost, currency = value, unit.upper()
                else:
                    # A monetary rate is not a consumed cost.
                    metrics[f"{name}_{unit.upper()}"] = value
            elif unit in ("credits", "s", "min"):
                metrics[("credits" if name == "total_usage" else name) if unit == "credits" else f"{name}_{unit}"] = value
            elif name == "usage_count":
                # Usage events are not documented as HTTP requests or generations.
                metrics[name] = value
            elif name in ("requests", "request_count", "generations", "generation_count"):
                metrics["requests" if "request" in name else "generations"] = value
        batch.rows.append(row(day, model=item.get("model"), service=item.get("product_type"), cost=cost, currency=currency, metrics=metrics))
        fields = batch.covered.setdefault(day, ["usage"])
        fields.extend(k for k in metrics if k in ("requests", "generations"))
        if cost is not None:
            fields.append("cost")
    batch.basis = "ElevenLabs analytics usage cost; subscription payments excluded"
    return batch


def byteplus_signed(env, host, service, region, action, version, payload, when=None):
    access = require(env, "BYTEPLUS_BILLING_ACCESS_KEY_ID")
    secret = require(env, "BYTEPLUS_BILLING_SECRET_ACCESS_KEY")
    stamp = (when or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    body = json.dumps(payload, separators=(",", ":")).encode()
    digest = hashlib.sha256(body).hexdigest()
    query = urlencode({"Action": action, "Version": version})
    signed = "host;x-content-sha256;x-date"
    canonical = f"POST\n/\n{query}\nhost:{host}\nx-content-sha256:{digest}\nx-date:{stamp}\n\n{signed}\n{digest}"
    scope = f"{stamp[:8]}/{region}/{service}/request"
    string = f"HMAC-SHA256\n{stamp}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
    signing = secret.encode()
    for part in (stamp[:8], region, service, "request"):
        signing = hmac.new(signing, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(signing, string.encode(), hashlib.sha256).hexdigest()
    return f"https://{host}/?{query}", {"Content-Type": "application/json", "Host": host, "X-Date": stamp,
        "X-Content-Sha256": digest, "Authorization": f"HMAC-SHA256 Credential={access}/{scope}, SignedHeaders={signed}, Signature={signature}"}, body


async def byteplus(client, env, start, end, category):
    batch = Batch("byteplus", category, start, end, time_zone="Asia/Shanghai", scope="BytePlus account")
    async def call(payload):
        usage = category == "usage"
        url, headers, body = byteplus_signed(env,
            "ark.ap-southeast-1.byteplusapi.com" if usage else "open.byteplusapi.com",
            "ark" if usage else "billing", "ap-southeast-1" if usage else "ap-singapore-1",
            "GetInferenceUsage" if usage else "ListBillDetail", "2024-01-01" if usage else "2022-01-01", payload)
        result = await request(client, "POST", url, headers=headers, content=body)
        if result.get("ResponseMetadata", {}).get("Error"):
            raise ReportingError("BytePlus could not return the requested report. Check billing access and reporting period.")
        return result["Result"]
    if category == "usage":
        result = await call({"StartTime": str(start), "EndTime": str(end), "QueryInterval": "Day",
            "Filters": [{"Key": "ModelName", "Values": []}, {"Key": "ModelVersion", "Values": []}]})
        fields = [f["Name"] for f in result["Fields"]]
        if int(result["DataCount"]) != len(result["Data"]):
            raise ReportingError("BytePlus returned an incomplete usage report; previous data retained.")
        for values in result["Data"]:
            item = dict(zip(fields, values, strict=True))
            day = iso_day(item["Day"])
            model = " / ".join(str(item[k]) for k in ("ModelName", "ModelVersion") if item.get(k)) or None
            metrics = {dst: item[src] for src, dst in {"InputTokens": "input_tokens", "OutputTokens": "output_tokens", "TotalTokens": "total_tokens", "ReqCnt": "requests"}.items() if src in item}
            batch.rows.append(row(day, model=model, metrics=metrics))
            batch.covered[day] = ["usage", *(["requests"] if "requests" in metrics else [])]
        return batch
    batch.basis = "Discounted bill amount; coupons and tax shown separately"
    months = sorted({str(d)[:7] for d in days(start, end)})
    for month in months:
        offset, seen = 0, set()
        while True:
            result = await call({"BillPeriod": month, "GroupPeriod": 1, "GroupTerm": 0, "Limit": 300,
                                 "Offset": offset, "NeedRecordNum": 1, "IgnoreZero": 0})
            for item in result["List"]:
                # Daily aggregates can share a BillDetailId across days/models.
                identity = identifier(json.dumps(item, sort_keys=True, default=str))
                if identity in seen:
                    continue
                seen.add(identity)
                raw_day = item["ExpenseDate"].replace("/", "-").split(" ")[0]
                day = date(*map(int, raw_day.split("-"))).isoformat()
                if not str(start) <= day <= str(end):
                    continue
                category_name = item.get("BillCategory", "")
                consume = category_name.startswith("consume-") or category_name == "Consumption-usage"
                cost = item.get("DiscountBillAmount") if consume else None
                adjustments = {"coupon": item.get("CouponAmount"), "tax": item.get("Tax")}
                if not consume:
                    adjustments[item.get("BillCategory", "adjustment")] = item.get("PayableAmount")
                # Preserve explicit model billing names without guessing a dated
                # inference version or assigning unrelated service configurations.
                billing_model = item.get("ConfigName") if item.get("Product") in ("Smart_Drawing_T2I", "ModelArk_video_generation") else None
                batch.rows.append(row(day, model=billing_model, cost=cost, currency=item.get("Currency"), service=item.get("Element") or item.get("Product"),
                    metrics={f'billed_{item["Unit"]}': item.get("Count")} if item.get("Unit") else {}, adjustments=adjustments))
                batch.covered[day] = ["cost", "usage"]
            offset += len(result["List"])
            if offset >= int(result["Total"]):
                break
            if not result["List"]:
                raise ReportingError("BytePlus returned incomplete bill pagination; previous data retained.")
    return batch


def google_billing(env, start, end):
    from google.cloud import bigquery
    from google.api_core.exceptions import NotFound
    table = require(env, "GOOGLE_BILLING_TABLE")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+\.[a-zA-Z0-9_]+\.[a-zA-Z0-9_]+", table):
        raise ReportingError("GOOGLE_BILLING_TABLE must be project.dataset.table.")
    client = bigquery.Client(project=env.get("GOOGLE_CLOUD_PROJECT"))
    query = f"""SELECT DATE(usage_start_time, 'UTC') day, project.id project_id,
        service.description service, sku.description sku, currency, cost_type,
        (SELECT l.value FROM UNNEST(labels) l WHERE l.key IN ('model_id', 'model')
          ORDER BY l.key DESC LIMIT 1) model,
        usage.unit unit, SUM(CAST(usage.amount AS NUMERIC)) quantity,
        SUM(CAST(cost AS NUMERIC)) cost,
        SUM(IFNULL((SELECT SUM(CAST(c.amount AS NUMERIC)) FROM UNNEST(credits) c), 0)) credits
        FROM `{table}` WHERE usage_start_time >= TIMESTAMP(@start)
        AND usage_start_time < TIMESTAMP(@end)
        GROUP BY day, project_id, service, sku, currency, cost_type, unit, model"""
    config = bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter("start", "DATE", start),
        bigquery.ScalarQueryParameter("end", "DATE", end + timedelta(days=1))], maximum_bytes_billed=int(env.get("GOOGLE_BILLING_MAX_BYTES", "10000000000")))
    batch = Batch("google", "costs", start, end, scope="Google billing export", basis="Usage charges before credits; credits and other adjustments separate")
    job = None
    try:
        job = client.query(query, job_config=config)
        for item in job.result(timeout=120):
            regular = item["cost_type"] == "regular"
            adjustments = {"credits": item["credits"]}
            if not regular:
                adjustments[item["cost_type"] or "adjustment"] = item["cost"]
            batch.rows.append(row(item["day"], model=item["model"], service=f'{item["service"]} · {item["sku"]}', cost=item["cost"] if regular else None,
                billing_service=item["service"], sku=item["sku"],
                currency=item["currency"], adjustments=adjustments, scope=item["project_id"],
                metrics={f'billed_{item["unit"]}': item["quantity"]} if regular and item["unit"] else {}))
            batch.covered[str(item["day"])] = ["cost", "usage"]
    except NotFound:
        raise ReportingError("Google billing export table is not available yet. Initial export can take up to five days; if export is already established, check the configured table. Automatic refresh will retry.") from None
    except TimeoutError:
        if job is not None:
            job.cancel()
        raise ReportingError("Google billing query timed out; previous data retained.") from None
    finally:
        client.close()
    if not batch.rows:
        batch.note = "No exported billing rows for this period yet. Initial export can take up to five days; unavailable amounts remain ×."
    return batch


async def minimax(client, env, start, end, category):
    key = require(env, "MINIMAX_USAGE_API_KEY")
    batch = Batch("minimax", "usage", start, end,
        scope="MiniMax V2 tasks visible to reporting key",
        basis="Provider-metered V2 task usage; no monetary cost reporting")
    batch.note = ("V2 video/Context-IR tasks only. API exposes the latest seven days; "
                  "collected daily aggregates are retained. Dates use task creation time in UTC. "
                  "Task counts are not HTTP request counts. Costs, balance, and payments are unavailable.")
    # The API has a rolling seven-day window. Exclude its partially expired UTC
    # boundary day so a later refresh cannot replace a cached full day with less data.
    first_day = max(start, datetime.now(timezone.utc).date() - timedelta(days=6))
    if end < first_day:
        return batch
    page, seen, grouped = 1, set(), {}
    while True:
        result = await request(client, "GET", "https://api.minimax.io/v2/query/video_generation",
            headers={"Authorization": f"Bearer {key}"}, params={"page_num": page, "page_size": 100})
        items, count = result["items"], int(result["total"])
        previous = len(seen)
        for item in items:
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            day = iso_day(item["created_at"])
            if not str(first_day) <= day <= str(end):
                continue
            task_type = item.get("task_type") or "unassigned_task_type"
            metrics = {"tasks": Decimal(1)}
            if task_type in ("generation", "regeneration"):
                metrics["generations"] = Decimal(item["status"] == "succeeded")
            units = {"input_seconds": "input_video_seconds", "output_seconds": "output_video_seconds",
                     "input_image_count": "input_images", "input_audio_seconds": "input_audio_seconds",
                     "prompt_tokens": "input_tokens", "completion_tokens": "output_tokens", "total_tokens": "total_tokens"}
            for name, unit in units.items():
                value = decimal((item.get("usage") or {}).get(name))
                if value is not None:
                    metrics[unit] = value
            identity = (day, item.get("model"), task_type)
            target = grouped.setdefault(identity, {})
            for unit, value in metrics.items():
                target[unit] = target.get(unit, Decimal(0)) + value
        if len(seen) >= count:
            break
        if len(seen) == previous:
            raise ReportingError("MiniMax returned incomplete task pagination; previous data retained.")
        page += 1
    for (day, model, task_type), metrics in grouped.items():
        batch.rows.append(row(day, model=model, service=task_type, metrics=metrics, scope=batch.scope))
        batch.covered.setdefault(day, ["usage"])
        if "generations" in metrics and "generations" not in batch.covered[day]:
            batch.covered[day].append("generations")
    return batch
