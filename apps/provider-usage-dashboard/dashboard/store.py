"""Reporting cache only. No source database or provider calls occur here."""
from __future__ import annotations

import copy
import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .model import identifier, now_iso


class MemoryStore:
    def __init__(self):
        self.data = {"days": {}, "status": {}, "balances": {}, "payments": {}, "meta": {}}
        self.mutex = threading.RLock()

    @contextmanager
    def transaction(self):
        with self.mutex:
            yield

    def read(self, start, end):
        with self.transaction():
            result = copy.deepcopy(self.data)
            result["days"] = [v for v in result["days"].values() if str(start) <= v["day"] <= str(end)]
            result["payments"] = [v for v in result["payments"].values() if str(start) <= v["day"] <= str(end)]
            return result

    def status(self):
        with self.transaction():
            return copy.deepcopy({"sources": self.data["status"], "collection": self.data["meta"].get("collection", {})})

    def save(self, batch):
        refreshed = now_iso()
        with self.transaction():
            for item in batch.snapshots(refreshed):
                self.data["days"][item["id"]] = item
            previous = self.data["status"].get(batch.key, {})
            self.data["status"][batch.key] = success_status(batch, refreshed, previous)
            if batch.balance is not None:
                self.data["balances"][batch.provider] = {**batch.balance, "as_of": refreshed}
            if batch.payments is not None:
                self.data["payments"] = {k: v for k, v in self.data["payments"].items()
                                         if not (v["provider"] == batch.provider and str(batch.start) <= v["day"] <= str(batch.end))}
                for item in batch.payments:
                    if str(batch.start) <= item["day"] <= str(batch.end):
                        self.data["payments"][identifier(batch.provider, item["id"])] = {**item, "provider": batch.provider}

    def failure(self, key, message, configured=True):
        with self.transaction():
            self.data["status"][key] = {**self.data["status"].get(key, {}), "state": "error" if configured else "not_configured",
                                       "message": message, "attempted_at": now_iso()}

    def claim(self, owner, minutes=25):
        with self.transaction():
            state = self.data["meta"].get("collection", {})
            if state.get("expires_at", "") > now_iso():
                return False
            self.data["meta"]["collection"] = {"owner": owner, "state": "running", "started_at": now_iso(),
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()}
            return True

    def finish(self, owner):
        with self.transaction():
            state = self.data["meta"].get("collection", {})
            if state.get("owner") == owner:
                state.update(state="idle", expires_at="", finished_at=now_iso())

    def meta(self, key, value=None):
        with self.transaction():
            if value is not None:
                self.data["meta"][key] = value
            return copy.deepcopy(self.data["meta"].get(key))

    def prune(self, cutoff):
        with self.transaction():
            for collection in ("days", "payments"):
                self.data[collection] = {k: v for k, v in self.data[collection].items() if v["day"] >= str(cutoff)}


def success_status(batch, refreshed, previous):
    observed = sorted(d for d, fields in batch.covered.items() if fields)
    return {"state": "updated", "message": batch.note, "last_success": refreshed, "attempted_at": refreshed,
            "earliest": min(filter(None, [previous.get("earliest"), observed[0] if observed else None]), default=None),
            "latest": max(filter(None, [previous.get("latest"), observed[-1] if observed else None]), default=None),
            "time_zone": batch.time_zone, "scope": batch.scope, "basis": batch.basis}


class FileStore(MemoryStore):
    """Local development only; atomic file updates also support a separate CLI collector."""
    def __init__(self, path):
        super().__init__()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def transaction(self):
        import fcntl
        with self.mutex, open(str(self.path) + ".lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.path.exists():
                self.data = json.loads(self.path.read_text())
            try:
                yield
                temporary = self.path.with_suffix(".tmp")
                temporary.write_text(json.dumps(self.data))
                temporary.chmod(0o600)
                temporary.replace(self.path)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


class FirestoreStore:
    def __init__(self, project, database):
        from google.cloud import firestore
        self.db = firestore.Client(project=project, database=database)

    def _docs(self, name):
        return self.db.collection(name)

    def _range(self, name, start, end):
        from google.cloud.firestore_v1.base_query import FieldFilter
        return self._docs(name).where(filter=FieldFilter("day", ">=", str(start))).where(filter=FieldFilter("day", "<=", str(end)))

    def read(self, start, end):
        return {"days": [d.to_dict() for d in self._range("days", start, end).stream()],
                "payments": [d.to_dict() for d in self._range("payments", start, end).stream()],
                "balances": {d.id: d.to_dict() for d in self._docs("balances").stream()},
                "status": {d.id: d.to_dict() for d in self._docs("status").stream()}, "meta": {}}

    def status(self):
        return {"sources": {d.id: d.to_dict() for d in self._docs("status").stream()},
                "collection": self.meta("collection") or {}}

    def save(self, batch):
        refreshed = now_iso()
        # A collector fetches every page before save. Each daily document is replaced
        # atomically; requests never see half of a day's models or a sum of revisions.
        writes = self.db.batch()
        count = 0
        for item in batch.snapshots(refreshed):
            writes.set(self._docs("days").document(item["id"]), item)
            count += 1
            if count == 400:
                writes.commit(); writes = self.db.batch(); count = 0
        if count:
            writes.commit()
        if batch.balance is not None:
            self._docs("balances").document(batch.provider).set({**batch.balance, "as_of": refreshed})
        if batch.payments is not None:
            current = {identifier(batch.provider, item["id"]): {**item, "provider": batch.provider}
                       for item in batch.payments if str(batch.start) <= item["day"] <= str(batch.end)}
            for doc in self._range("payments", batch.start, batch.end).stream():
                if doc.to_dict()["provider"] == batch.provider and doc.id not in current:
                    doc.reference.delete()
            for key, value in current.items():
                self._docs("payments").document(key).set(value)
        ref = self._docs("status").document(batch.key)
        ref.set(success_status(batch, refreshed, ref.get().to_dict() or {}))

    def failure(self, key, message, configured=True):
        self._docs("status").document(key).set({"state": "error" if configured else "not_configured",
            "message": message, "attempted_at": now_iso()}, merge=True)

    def claim(self, owner, minutes=25):
        from google.cloud import firestore
        ref = self._docs("meta").document("collection")
        @firestore.transactional
        def acquire(transaction):
            state = ref.get(transaction=transaction).to_dict() or {}
            if state.get("expires_at", "") > now_iso():
                return False
            transaction.set(ref, {"owner": owner, "state": "running", "started_at": now_iso(),
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()})
            return True
        return acquire(self.db.transaction())

    def finish(self, owner):
        from google.cloud import firestore
        ref = self._docs("meta").document("collection")
        @firestore.transactional
        def release(transaction):
            state = ref.get(transaction=transaction).to_dict() or {}
            if state.get("owner") == owner:
                transaction.update(ref, {"state": "idle", "expires_at": "", "finished_at": now_iso()})
        release(self.db.transaction())

    def meta(self, key, value=None):
        ref = self._docs("meta").document(key)
        if value is not None:
            ref.set({"value": value})
        data = ref.get().to_dict() or {}
        return data if key == "collection" else data.get("value")

    def prune(self, cutoff):
        from google.cloud.firestore_v1.base_query import FieldFilter
        for name in ("days", "payments"):
            for doc in self._docs(name).where(filter=FieldFilter("day", "<", str(cutoff))).stream():
                doc.reference.delete()


def make_store():
    if os.getenv("K_SERVICE") or os.getenv("CLOUD_RUN_JOB") or os.getenv("DASHBOARD_STORE") == "firestore":
        return FirestoreStore(os.environ["GOOGLE_CLOUD_PROJECT"], os.getenv("FIRESTORE_DATABASE", "provider-usage-dashboard"))
    return FileStore(os.getenv("DASHBOARD_CACHE_PATH", ".local/reporting.json"))
