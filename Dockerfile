FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken \
    HF_HOME=/opt/hf

WORKDIR /app
COPY pyproject.toml uv.lock ./
# The "router" extra (CPU-only torch + sentence-transformers) powers model="auto".
RUN uv sync --frozen --no-dev --extra router --no-install-project

COPY gateway ./gateway
COPY migrations ./migrations
COPY alembic.ini models.yaml README.md ./
# Bake the tokenizer and the embedding model into the image: no downloads at startup.
RUN uv sync --frozen --no-dev --extra router \
    && python -c "import tiktoken; tiktoken.get_encoding('o200k_base')" \
    && python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-en-v1.5')"
ENV HF_HUB_OFFLINE=1

EXPOSE 8000
# Migrations run once in the `migrate` compose service, not in every replica (they'd race).
CMD ["uvicorn", "gateway.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "30", "--timeout-keep-alive", "75", "--no-access-log"]
