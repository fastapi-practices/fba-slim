# === Python environment from uv ===
FROM ghcr.io/astral-sh/uv:python3.10-bookworm-slim AS builder

# Used for build Python packages
RUN sed -i 's/deb.debian.org/mirrors.ustc.edu.cn/g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends gcc python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY . /fba

WORKDIR /fba

# Configure uv environment
ENV UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local

# Install dependencies with cache
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-default-groups --group server --no-install-project

# Preinstall plugin dependencies
RUN --mount=type=cache,target=/root/.cache/uv \
    python -c "from backend.plugin.requirements import install_requirements; install_requirements(None)"

# === Runtime server image ===
FROM python:3.10-slim-bookworm

RUN sed -i 's/deb.debian.org/mirrors.ustc.edu.cn/g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates supervisor \
    && rm -rf /var/lib/apt/lists/*

ADD https://astral.sh/uv/install.sh /uv-installer.sh

RUN sh /uv-installer.sh && rm /uv-installer.sh

ENV PATH="/root/.local/bin/:$PATH"

COPY --from=builder /fba /fba

COPY --from=builder /usr/local /usr/local

COPY deploy/backend/supervisor/supervisord.conf /etc/supervisor/supervisord.conf
COPY deploy/backend/supervisor/fba_server.conf /etc/supervisor/conf.d/

RUN mkdir -p /var/log/fba

EXPOSE 8001

CMD ["supervisord", "-c", "/etc/supervisor/supervisord.conf"]
