FROM docker.litellm.ai/berriai/litellm:main-v1.83.14-stable

WORKDIR /app

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
COPY gateway_server.py /app/gateway_server.py
COPY callback_server.py /app/callback_server.py
COPY migrations /app/migrations

# Ensure unbuffered logs
ENV PYTHONUNBUFFERED=1
ENV CONFIG_FILE_PATH=/app/litellm_config.yaml

# Cloud Run sets PORT; default to 8080 for local use
ENV PORT=8080

# Base image ENTRYPOINT runs `litellm "$@"`; a shell-wrapped CMD becomes `litellm sh -c ...` and breaks.
# Override ENTRYPOINT so we can expand $PORT and bind on all interfaces for Cloud Run.
ENTRYPOINT ["/bin/sh", "-c"]
# Use LiteLLM's FastAPI app and lifespan, adding only the durable generation router.
CMD ["exec uvicorn gateway_server:app --host 0.0.0.0 --port ${PORT:-8080}"]
