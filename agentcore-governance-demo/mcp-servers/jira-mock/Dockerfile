FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app

ENV UV_SYSTEM_PYTHON=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_PROGRESS=1 \
    PYTHONUNBUFFERED=1

RUN useradd -m -u 1000 bedrock_agentcore

COPY pyproject.toml ./
RUN uv pip install --system fastmcp uvicorn

COPY --chown=bedrock_agentcore:bedrock_agentcore . .

USER bedrock_agentcore

EXPOSE 8000

CMD ["python", "-m", "main"]
