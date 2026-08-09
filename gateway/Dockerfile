# The gateway image. Only `gateway/` goes in it: the pipeline, the voice and
# the models stay on the Mac, and the cluster holds nothing that could
# reproduce any of them.
#
# Two stages. The builder resolves the venv with uv and is thrown away; the
# runtime stage gets the venv and the source and nothing else, so neither uv
# nor a pip cache nor a compiler ships to a public registry. Both stages sit on
# the same base, which is what makes the copied venv's interpreter the same
# interpreter that runs it.
#
# The base is pinned by digest as well as by tag. The tag is what a human
# reads; the digest is what makes a rebuild in three months produce the same
# image, and Dependabot's docker ecosystem bumps both together.

# renovate/dependabot: docker
FROM python:3.14-slim@sha256:a7fb1e634c4a578f9e0bd6327f11a3cde11b7a9395f48e24360c0988bcc5c2bc AS base

FROM ghcr.io/astral-sh/uv:0.12.3@sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc AS uv

# --- builder ----------------------------------------------------------------
FROM base AS builder

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

# requirements.txt is compiled from requirements.in and pins every transitive
# dependency, so this layer is reproducible and only reinstalls when the pins
# actually move. A cache mount keeps the wheels out of the layer entirely.
COPY gateway/requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/venv \
 && uv pip install --python /opt/venv/bin/python --no-cache -r /tmp/requirements.txt

# --- runtime ----------------------------------------------------------------
FROM base AS runtime

LABEL org.opencontainers.image.title="reelsmith-gateway" \
      org.opencontainers.image.description="Comment to DM gateway and scheduled publish queue for reelsmith" \
      org.opencontainers.image.source="https://github.com/mortennordbye/reelsmith" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    VIRTUAL_ENV=/opt/venv

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY gateway/ gateway/

# The state directory is a mount in every real deployment. Creating it here
# means a bare `docker run` still starts rather than dying on a missing path.
ENV GATEWAY_DB_PATH=/state/gateway.sqlite3 \
    GATEWAY_COVERS_DIR=/state/covers

# Nothing in here needs root, and the SQLite file lives on a mount that the
# cluster will own. One layer, because each RUN is a layer and these facts are
# one decision.
#
# pip goes too. The venv was built by uv in the builder stage and nothing at
# runtime installs anything, but the base image's pip vendors its own copies of
# setuptools and msgpack, and a scanner reads those as two HIGH findings
# against a public image. Deleting it removes the findings and the ~7 MB rather
# than explaining every quarter why they do not apply. The glob rather than a
# literal python3.14 path, because Dependabot moves this base and a hardcoded
# version would silently stop matching.
RUN useradd --create-home --uid 10001 gateway \
 && mkdir -p /state/covers \
 && chown -R gateway:gateway /state \
 && rm -rf /usr/local/lib/python3.*/site-packages/pip* /usr/local/bin/pip*
USER 10001

EXPOSE 8000

# Kubernetes uses its own probes and ignores this, but `docker run` and the
# compose file do not, and /healthz needs no auth precisely so a probe can
# reach it. urllib rather than curl, because the slim base ships no curl and
# adding one to answer a health check is a whole apt layer.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]

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
