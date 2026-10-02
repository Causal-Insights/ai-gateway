"""Deterministic sample data, clearly labeled and never enabled in Cloud Run."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .model import Batch, PROVIDERS, days, row
from .store import MemoryStore


def demo_store():
    store = MemoryStore()
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=89)
    models = {"openai": ["gpt-6-astra", "gpt-image-2"], "xai": ["grok-4.20", "grok-imagine-video"],
              "byteplus": ["seedance-2-0", "dola-seedream-5-0-pro-260628"],
              "elevenlabs": ["eleven_multilingual_v2", "music_v2_5"], "google": ["gemini-3.1-pro", "veo-3.1"], "minimax": ["MiniMax-H3"]}
    bases = [Decimal("18.40"), Decimal("9.20"), Decimal("7.30"), None, Decimal("13.70"), None]
    for index, provider in enumerate(PROVIDERS):
        batch = Batch(provider, "demo", start, today, time_zone="Asia/Shanghai" if provider == "byteplus" else "UTC", scope="Sample account")
        for i, day in enumerate(days(start, today)):
            amount = bases[index] * Decimal(str(0.75 + (i % 7) / 12)) if bases[index] else None
            for j, model in enumerate(models[provider]):
                cost = amount * (Decimal("0.7") if j == 0 else Decimal("0.3")) if amount is not None else None
                metrics = {"requests": (90 + i * 3 + index * 11) * (j + 1)}
                metrics.update({"credits": 7500 + i * 200} if provider == "elevenlabs" else {"input_tokens": 180000 + i * 4700} if j == 0 else {"generations": 12 + i % 9, "video_seconds": 72 + i % 27})
                if provider == "minimax":
                    metrics = {"tasks": 3, "generations": 3, "output_video_seconds": 18}
                batch.rows.append(row(day, model=model, cost=cost, metrics=metrics))
            batch.covered[str(day)] = ["usage", "requests", "generations"] + (["cost"] if amount is not None else [])
        if provider == "minimax":
            batch.covered = {day: ["usage", "generations"] for day in batch.covered}
            batch.note = "Sample V2 task usage only; costs, balance and payments unavailable."
        if provider == "xai":
            batch.balance = {"amount": "184.62", "unit": "USD", "label": "Prepaid balance"}
            batch.payments = [{"id": "demo-topup", "day": str(today - timedelta(days=2)), "amount": "250", "currency": "USD", "kind": "topup", "description": "Prepaid credit purchase"}]
        if provider == "elevenlabs":
            batch.balance = {"amount": "382140", "unit": "credits", "label": "Remaining subscription allowance"}
        store.save(batch)
        for source in ("gateway", "litellm", "magiclens"):
            internal = Batch(provider, "costs", start, today, source=source,
                scope="Magic Lens activity" if source == "magiclens" else "Gateway activity")
            internal.rows = [{**r, "cost": str(Decimal(r["cost"]) * (Decimal("0.94") if source == "magiclens" else Decimal(1))) if r["cost"] else None} for r in batch.rows]
            internal.covered = batch.covered
            store.save(internal)
    return store
