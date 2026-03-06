# Stage 1: build virtualenv
FROM ghcr.io/astral-sh/uv:python3.13-alpine AS builder
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Stage 2: minimal runtime
FROM python:3.13-alpine
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
ENV PATH="/app/.venv/bin:$PATH"
RUN addgroup -S bridge && adduser -S bridge -G bridge
USER bridge
CMD ["python", "-m", "trip_updogger.main"]
