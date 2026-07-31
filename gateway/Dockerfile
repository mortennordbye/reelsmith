# The gateway image. Only `gateway/` goes in it: the pipeline, the voice and
# the models stay on the Mac, and the cluster holds nothing that could
# reproduce any of them.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first, so editing a route does not reinstall the world.
COPY gateway/requirements.txt gateway/requirements.txt
RUN pip install --no-cache-dir -r gateway/requirements.txt

COPY gateway/ gateway/

# The state directory is a mount in every real deployment. Creating it here
# means a bare `docker run` still starts rather than dying on a missing path.
RUN mkdir -p /state/covers
ENV GATEWAY_DB_PATH=/state/gateway.sqlite3 \
    GATEWAY_COVERS_DIR=/state/covers

# Nothing in here needs root, and the SQLite file lives on a mount that the
# cluster will own.
RUN useradd --create-home --uid 10001 gateway && chown -R gateway /state
USER gateway

EXPOSE 8000
CMD ["uvicorn", "gateway.app:app", "--host", "0.0.0.0", "--port", "8000"]
