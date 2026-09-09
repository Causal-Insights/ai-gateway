FROM ghcr.io/berriai/litellm:v1.95.0@sha256:50e647bd5ee32010317378335d5830dbbcd793b4dd1a9a4460bd34a9272cda95

WORKDIR /app

# LiteLLM 1.95 is Wolfi (apk), not Debian. ffprobe is optional at runtime
# (adapters return {} when it is missing) but Veo media inspection uses it.
RUN apk add --no-cache ffmpeg

COPY requirements.txt /app/requirements.txt
RUN python -m ensurepip && python -m pip install --no-cache-dir -r /app/requirements.txt

# Copy configuration and custom handlers into the image
COPY litellm_config.yaml /app/litellm_config.yaml
COPY custom_handler.py /app/custom_handler.py
COPY custom_handler_audio.py /app/custom_handler_audio.py
COPY custom_handler_common.py /app/custom_handler_common.py
COPY legacy_usage.py /app/legacy_usage.py
COPY custom_handler_xai.py /app/custom_handler_xai.py
COPY custom_handler_seedance.py /app/custom_handler_seedance.py
COPY custom_handler_seedream.py /app/custom_handler_seedream.py
COPY generation_job_models.py /app/generation_job_models.py
COPY generation_job_repository.py /app/generation_job_repository.py
COPY generation_job_adapters.py /app/generation_job_adapters.py
COPY generation_job_scheduler.py /app/generation_job_scheduler.py
COPY generation_job_routes.py /app/generation_job_routes.py
COPY gateway_request_policy.py /app/gateway_request_policy.py
COPY gateway_logging.py /app/gateway_logging.py
COPY gateway_healthcheck.py /app/gateway_healthcheck.py
COPY gateway_server.py /app/gateway_server.py
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
