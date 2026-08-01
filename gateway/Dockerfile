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
# --proxy-headers with a wildcard allow-list, because TLS terminates at Traefik
# and without it the app believes every request arrived over plain http on the
# pod address. That is not cosmetic: `url_for` then builds the admin login
# form's action as http://, so the browser warns the password is being sent
# insecurely, and submitting it becomes a cross-scheme POST that the CSRF check
# correctly refuses. The panel is unusable and both symptoms point at the wrong
# thing.
#
# The wildcard is safe here specifically: the CiliumNetworkPolicy admits port
# 8000 only from the Traefik pods and the kubelet, so there is no client that
# could forge these headers without already being inside the cluster.
CMD ["uvicorn", "gateway.app:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
