"""Explicit AI Gateway server entrypoint that preserves LiteLLM's application."""

from contextlib import asynccontextmanager

from gateway_logging import configure_gateway_logging


configure_gateway_logging()

from litellm.proxy._types import LiteLLMRoutes
from litellm.proxy.proxy_server import app

from generation_job_routes import router as generation_jobs_router
from generation_job_repository import repository
from generation_job_scheduler import start_local_scheduler, stop_local_scheduler
from gateway_request_policy import GatewayRequestPolicyMiddleware


GENERATION_JOB_LLM_ROUTES = (
    "/v1/generation-jobs",
    "/v1/generation-jobs/{job_id}",
    "/v1/generation-jobs/{job_id}/content",
)


def register_generation_job_llm_routes() -> None:
    """Classify durable generation endpoints as inference routes for virtual keys.

    LiteLLM evaluates ``allowed_routes=['llm_api_routes']`` before invoking the
    FastAPI dependency attached to our router. Custom application routes are
    otherwise treated as management APIs and rejected even when the key is a
    valid inference key. Both collections are mutable snapshots in LiteLLM, so
    register with the aggregate permission list and the inference-role check.
    """
    for route in GENERATION_JOB_LLM_ROUTES:
        if route not in LiteLLMRoutes.openai_routes.value:
            LiteLLMRoutes.openai_routes.value.append(route)
        if route not in LiteLLMRoutes.llm_api_routes.value:
            LiteLLMRoutes.llm_api_routes.value.append(route)


register_generation_job_llm_routes()
app.include_router(generation_jobs_router)
app.add_middleware(GatewayRequestPolicyMiddleware)

_litellm_lifespan = app.router.lifespan_context


@asynccontextmanager
async def _gateway_lifespan(application):
    async with _litellm_lifespan(application):
        await repository.pool()
        await start_local_scheduler()
        try:
            yield
        finally:
            await stop_local_scheduler()
            await repository.close()


app.router.lifespan_context = _gateway_lifespan
