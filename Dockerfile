FROM python:3.12-slim

# ffmpeg (software x264) for the encode worker.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/ ./server/
COPY migrations/ ./migrations/
COPY web/ ./web/
COPY scripts/ ./scripts/

ENV PORT=8090 \
    DATA_ROOT=/data/mikevideo \
    INGEST_DATA_ROOT=/data/mikevideo/ingest

EXPOSE 8090

# Single worker: the encode queue + asyncpg pool live in-process.
CMD ["sh", "-c", "uvicorn server.app:app --host 0.0.0.0 --port ${PORT} --timeout-keep-alive 120"]
