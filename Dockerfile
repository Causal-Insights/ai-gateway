FROM ghcr.io/berriai/litellm:v1.102.1@sha256:87f34979b9f8cb274fac90ca8a4fdda07d8480de22755562a26adeb95ce20d02

WORKDIR /app

# The pinned LiteLLM image uses Wolfi (apk). ffprobe is optional at runtime
# (adapters return {} when it is missing) but Veo media inspection uses it.
RUN apk add --no-cache ffmpeg

COPY requirements.txt /app/requirements.txt
RUN python -m ensurepip && python -m pip install --no-cache-dir -r /app/requirements.txt

# Keep the generated ORM consistent with migration 004. Unknown spend must be
# readable by the stock LiteLLM Logs API without coercing it to zero.
COPY scripts/patch_execution_log_schema.py /app/patch_execution_log_schema.py
RUN python /app/patch_execution_log_schema.py /app/schema.prisma && prisma generate --schema=/app/schema.prisma

# Copy configuration and custom handlers into the image
COPY litellm_config.yaml /app/litellm_config.yaml
COPY custom_handler.py /app/custom_handler.py
COPY custom_handler_audio.py /app/custom_handler_audio.py
COPY custom_handler_common.py /app/custom_handler_common.py
COPY legacy_usage.py /app/legacy_usage.py
COPY custom_handler_xai.py /app/custom_handler_xai.py
COPY custom_handler_seedance.py /app/custom_handler_seedance.py
COPY custom_handler_seedream.py /app/custom_handler_seedream.py
COPY grok_video_contract.py /app/grok_video_contract.py
COPY seedance_video_contract.py /app/seedance_video_contract.py
COPY generation_job_models.py /app/generation_job_models.py
COPY generation_job_repository.py /app/generation_job_repository.py
COPY generation_job_adapters.py /app/generation_job_adapters.py
COPY video_capabilities.py expanded_video_adapters.py capability_discovery.py /app/
COPY generation_job_scheduler.py /app/generation_job_scheduler.py
COPY generation_job_routes.py /app/generation_job_routes.py
COPY gateway_request_policy.py /app/gateway_request_policy.py
COPY openai_model_contracts.py /app/openai_model_contracts.py
COPY image_stream_routes.py /app/image_stream_routes.py
COPY openai_usage.py /app/openai_usage.py
COPY gateway_logging.py /app/gateway_logging.py
COPY gateway_healthcheck.py /app/gateway_healthcheck.py
COPY gateway_server.py /app/gateway_server.py
COPY voice_resources.py /app/voice_resources.py
COPY advanced_audio.py /app/advanced_audio.py
COPY hosted_tools.py /app/hosted_tools.py
COPY grounded_pricing.py /app/grounded_pricing.py
COPY owned_openai_resources.py /app/owned_openai_resources.py
COPY openai_owner_lifecycle.py /app/openai_owner_lifecycle.py
COPY native_vertex.py /app/native_vertex.py
COPY pricing_registry.py litellm_pricing.py accounting_usage.py cost_accounting.py gateway_accounting.py cost_repairs.py /app/
COPY pricing /app/pricing
COPY gateway_entrypoint.sh /app/gateway_entrypoint.sh
COPY callback_server.py /app/callback_server.py
COPY migrations /app/migrations

RUN chmod 0755 /app/gateway_entrypoint.sh

# Ensure unbuffered logs
ENV PYTHONUNBUFFERED=1
ENV CONFIG_FILE_PATH=/app/litellm_config.yaml

# Cloud Run sets PORT; default to 8080 for local use
ENV PORT=8080

# Keep migrations an explicit no-traffic action, then start LiteLLM's FastAPI
# application with the gateway's durable routes and request policies attached.
ENTRYPOINT ["/app/gateway_entrypoint.sh"]
CMD []
