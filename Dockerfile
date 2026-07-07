FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY src ./src
COPY webui ./webui
COPY data/prompts ./data/prompts

EXPOSE 8358

CMD ["uv", "run", "--frozen", "uvicorn", "webui.server:app", "--host", "0.0.0.0", "--port", "8358"]
