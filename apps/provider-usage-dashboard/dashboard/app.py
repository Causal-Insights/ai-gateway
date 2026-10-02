import asyncio
import os
import logging
import hashlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .model import PROVIDERS, months_before, safe_error
from .report import build_report
from .store import make_store

ROOT = Path(__file__).parent


def create_app(store=None, *, mode=None, refresh=None):
    mode = mode or ("cloud" if os.getenv("K_SERVICE") else "local")
    app = FastAPI(title="Provider Usage Dashboard", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.collection_task = None
    templates = Jinja2Templates(directory=str(ROOT / "templates"))
    asset_version = hashlib.sha256((ROOT / "static/dashboard.js").read_bytes() + (ROOT / "static/dashboard.css").read_bytes()).hexdigest()[:12]
    if mode == "cloud":
        from google.auth.transport.requests import Request as GoogleRequest
        import requests
        app.state.auth_request = GoogleRequest(session=requests.Session())

    def cache():
        if app.state.store is None:
            app.state.store = make_store()
        return app.state.store

    @app.middleware("http")
    async def protect(request, call_next):
        if request.url.path != "/healthz":
            if mode == "cloud":
                from google.oauth2 import id_token
                assertion = request.headers.get("x-goog-iap-jwt-assertion")
                audience = os.getenv("IAP_AUDIENCE")
                if not assertion or not audience:
                    return JSONResponse({"detail": "Sign in through the authorized dashboard URL."}, status_code=401)
                try:
                    claims = await asyncio.to_thread(id_token.verify_token, assertion, app.state.auth_request,
                        audience=audience, certs_url="https://www.gstatic.com/iap/verify/public_key")
                    if claims.get("iss") != "https://cloud.google.com/iap":
                        raise ValueError("issuer")
                except Exception:
                    return JSONResponse({"detail": "Invalid dashboard authentication."}, status_code=401)
            elif request.client and request.client.host not in ("127.0.0.1", "::1", "testclient"):
                return JSONResponse({"detail": "Local mode is available on loopback only."}, status_code=403)
            if request.method == "POST":
                origin = request.headers.get("origin")
                if origin and urlparse(origin).netloc != request.url.netloc:
                    return JSONResponse({"detail": "Cross-origin refresh is not allowed."}, status_code=403)
                if request.headers.get("x-dashboard-request") != "1":
                    return JSONResponse({"detail": "Missing dashboard request header."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") or request.url.path == "/" else "private, max-age=3600"
        return response

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.get("/")
    def index(request: Request):
        return templates.TemplateResponse(request=request, name="index.html", context={"demo": mode == "demo", "providers": PROVIDERS, "asset_version": asset_version})

    @app.get("/api/report")
    def report(start: date | None = None, end: date | None = None, providers: str | None = None):
        today = datetime.now(timezone.utc).date()
        start, end = start or today - timedelta(days=6), end or today
        if start > end or start < months_before(today, 13) or end > today:
            raise HTTPException(400, "Choose an ordered date range within the last 13 months, ending no later than today.")
        selected = providers.split(",") if providers else list(PROVIDERS)
        if not selected or any(p not in PROVIDERS for p in selected):
            raise HTTPException(400, "Unknown provider filter.")
        try:
            data = cache().read(min(start, today - timedelta(days=31)), today + timedelta(days=1))
        except Exception as exc:
            raise HTTPException(503, safe_error(exc)) from None
        result = build_report(data, start, end, list(dict.fromkeys(selected)))
        result["demo"] = mode == "demo"
        return result

    @app.get("/api/status")
    def status():
        try:
            return cache().status()
        except Exception as exc:
            raise HTTPException(503, safe_error(exc)) from None

    @app.post("/api/refresh", status_code=202)
    async def trigger():
        if mode == "demo":
            return {"state": "demo", "message": "Sample data only. No provider calls were made."}
        try:
            if refresh:
                return await refresh()
            if mode == "cloud":
                from google.cloud import run_v2
                job = os.environ["DASHBOARD_SYNC_JOB"]
                client = run_v2.JobsClient()
                # Submitting a job returns an operation; don't wait for collection.
                operation = await asyncio.to_thread(client.run_job, name=job)
                return {"state": "queued", "operation": operation.operation.name}
            from .collector import run
            if app.state.collection_task and not app.state.collection_task.done():
                return {"state": "already_running"}
            async def collect_locally():
                try:
                    await run(cache())
                except Exception as exc:
                    logging.getLogger("dashboard.collector").error("collection_failed %s", safe_error(exc))
            app.state.collection_task = asyncio.create_task(collect_locally())
            return {"state": "queued"}
        except Exception as exc:
            raise HTTPException(503, safe_error(exc)) from None

    app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")
    return app
