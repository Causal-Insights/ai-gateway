FROM docker.litellm.ai/berriai/litellm:main-v1.83.7-stable.patch.1

WORKDIR /app

# Copy configuration and custom handlers into the image
COPY litellm_config.yaml /app/litellm_config.yaml
COPY custom_handler.py /app/custom_handler.py

# Ensure unbuffered logs
ENV PYTHONUNBUFFERED=1

# Cloud Run sets PORT; default to 8080 for local use
ENV PORT=8080

# Base image ENTRYPOINT runs `litellm "$@"`; a shell-wrapped CMD becomes `litellm sh -c ...` and breaks.
# Override ENTRYPOINT so we can expand $PORT and bind on all interfaces for Cloud Run.
ENTRYPOINT ["/bin/sh", "-c"]
# Cloud Run health checks hit 0.0.0.0:$PORT; bind explicitly (LiteLLM defaults host to 127.0.0.1).
CMD ["exec litellm --config /app/litellm_config.yaml --host 0.0.0.0 --port ${PORT:-8080}"]

