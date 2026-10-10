FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY gateway ./gateway
COPY migrations ./migrations
COPY alembic.ini models.yaml README.md ./
RUN uv sync --frozen --no-dev \
    && python -c "import tiktoken; tiktoken.get_encoding('o200k_base')"

EXPOSE 8000
# Migrations run once in the `migrate` compose service, not in every replica (they'd race).
CMD ["uvicorn", "gateway.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "30", "--timeout-keep-alive", "75", "--no-access-log"]
