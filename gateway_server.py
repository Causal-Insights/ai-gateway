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
from openai_model_contracts import install_image_adapters
from gateway_accounting import AccountingMiddleware, install as install_accounting, router as cost_router
from voice_resources import install as install_voice_access, router as voice_router
from advanced_audio import router as advanced_audio_router, SpeechOptionsMiddleware
from capability_discovery import router as capability_router
from owned_openai_resources import OwnedOpenAIResourcesMiddleware
from openai_owner_lifecycle import install_budget_exemption
from native_vertex import router as native_vertex_router

install_image_adapters()
install_accounting()
install_voice_access()
install_budget_exemption()


GENERATION_JOB_LLM_ROUTES = (
    "/v1/generation-jobs",
    "/v1/generation-jobs/{job_id}",
    "/v1/generation-jobs/{job_id}/content",
    "/v1/generation-jobs/{job_id}/outputs/{output_index}",
    "/v1/costs/{accounting_id}",
    "/v1/capabilities",
    "/v1/voices",
    "/v1/voices/{voice_id}",
    "/v1/audio/dialogue", "/v1/audio/speech-with-timestamps", "/v1/audio/generations", "/v1/audio/music/plan",
    "/v1/audio/requests/{request_id}", "/v1/songs", "/v1/songs/{song_id}", "/v1/songs/{song_id}/content", "/v1/songs/{song_id}/takes",
    "/v1/audio/sessions", "/v1/audio/sessions/{session_id}/stream",
    "/v1/audio/owner-data",
    "/v1/files", "/v1/files/{file_id}", "/v1/files/{file_id}/content",
    "/v1/resources/owner-data",
    "/v1/batches", "/v1/batches/{batch_id}", "/v1/batches/{batch_id}/cancel", "/v1/batches/{batch_id}/recover",
    "/v1/vector_stores", "/v1/vector_stores/{vector_store_id}", "/v1/vector_stores/{vector_store_id}/files",
    "/v1/vector_stores/{vector_store_id}/files/{file_id}", "/v1/vector_stores/{vector_store_id}/files/{file_id}/content",
    "/v1/containers", "/v1/containers/{container_id}", "/v1/containers/{container_id}/files",
    "/v1/containers/{container_id}/files/{file_id}", "/v1/containers/{container_id}/files/{file_id}/content",
    "/v1/models/{model}/count-tokens",
    "/v1/interactions", "/v1/interactions/{interaction_id}",
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
app.include_router(cost_router)
app.include_router(voice_router)
app.include_router(advanced_audio_router)
app.include_router(capability_router)
app.include_router(native_vertex_router)
app.add_middleware(GatewayRequestPolicyMiddleware)
app.add_middleware(SpeechOptionsMiddleware)
app.add_middleware(OwnedOpenAIResourcesMiddleware)
app.add_middleware(AccountingMiddleware)

_litellm_lifespan = app.router.lifespan_context


@asynccontextmanager
async def _gateway_lifespan(application):
    async with _litellm_lifespan(application):
        await repository.pool()
        # LiteLLM loads configured callbacks during its lifespan.
        install_accounting()
        install_voice_access()
        await start_local_scheduler()
        try:
            yield
        finally:
            await stop_local_scheduler()
            await repository.close()


app.router.lifespan_context = _gateway_lifespan
