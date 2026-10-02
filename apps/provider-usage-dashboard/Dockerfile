FROM python:3.13-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8080
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=10001:10001 dashboard ./dashboard
USER 10001:10001
ENTRYPOINT ["python", "-m", "dashboard"]
CMD ["serve"]
