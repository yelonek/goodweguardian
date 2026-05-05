FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY . .
RUN uv sync --frozen && uv pip install debugpy

ENV PATH="/app/.venv/bin:$PATH"

# Defaults overridden by docker-compose
CMD ["python", "hourly_balance_run.py"]
