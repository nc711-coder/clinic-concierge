# Clinic Concierge API + demo site, for Google Cloud Run (or any Docker host).
#   docker build -t clinic-concierge .
#   docker run -p 8080:8080 --env-file .env clinic-concierge
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FASTEMBED_CACHE_PATH=/models \
    HF_HUB_DISABLE_TELEMETRY=1

WORKDIR /app

# Dependencies first: this layer is cached until requirements.txt changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Only what the server needs at runtime (no source documents, tests or evals).
COPY ingest/ ingest/
COPY app/ app/
COPY sql/ sql/
COPY web/ web/
COPY data/schedule.json data/schedule.json

# Bake the embedding + reranking models into the image.
RUN python -m app.warm

# Don't run as root.
RUN useradd --create-home appuser && chown -R appuser /app /models
USER appuser

# Cloud Run tells the app which port to use in $PORT (8080 by default).
EXPOSE 8080
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
