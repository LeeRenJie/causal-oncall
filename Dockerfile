# syntax=docker/dockerfile:1.7
#
# Causal On-Call — Cloud Run image.
#
# The Dynatrace MCP server is distributed as `@dynatrace-oss/dynatrace-mcp`
# and runs only via `npx`, so the container needs Node 20 in addition to
# Python 3.12. We use a two-stage build: stage 1 installs Python deps into
# a virtual env (so we can copy a clean tree forward); stage 2 layers the
# venv on top of a slim runtime that also carries Node.

# ---------- Stage 1: Python builder ----------
FROM python:3.12-slim AS python-builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install .

# ---------- Stage 2: runtime (Python + Node) ----------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:${PATH}" \
    PORT=8080 \
    NODE_MAJOR=20

# Install Node 20 LTS from NodeSource. The MCP server is invoked via npx
# at runtime; the package itself is fetched on first use and then cached
# under /root/.npm inside the container.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && mkdir -p /etc/apt/keyrings \
 && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
      | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
 && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
      > /etc/apt/sources.list.d/nodesource.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

# Copy the prebuilt Python venv from stage 1.
COPY --from=python-builder /opt/venv /opt/venv

# Copy app source last so changes here don't bust the deps layer.
WORKDIR /app
COPY src ./src
COPY pyproject.toml README.md LICENSE ./

# Cloud Run sends SIGTERM; uvicorn handles it cleanly with --no-server-header.
EXPOSE 8080
CMD ["uvicorn", "causal_oncall.app:app", "--host", "0.0.0.0", "--port", "8080"]
