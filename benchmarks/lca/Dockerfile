FROM python:3.10-slim@sha256:31dd4d9529d02d7436659061cb7564cd4733fc90e5e152709a942d53382ec8d0
COPY --from=ghcr.io/astral-sh/uv:0.8.22@sha256:9874eb7afe5ca16c363fe80b294fe700e460df29a55532bbfea234a0f12eddb1 /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends git bash \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.lock /tmp/requirements.lock
RUN uv venv /opt/httpx && uv pip install --python /opt/httpx/bin/python \
    --require-hashes -r /tmp/requirements.lock \
    && uv venv /opt/cloud-init && uv pip install --python /opt/cloud-init/bin/python ruff==0.0.285
ENV PATH="/opt/httpx/bin:$PATH"
WORKDIR /workspace
