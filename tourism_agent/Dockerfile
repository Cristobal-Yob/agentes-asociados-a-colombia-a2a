FROM ghcr.io/astral-sh/uv:0.12.13 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY common ./common
COPY agenda_agent/__init__.py agenda_agent/contracts.py ./agenda_agent/
COPY tourism_agent ./tourism_agent
RUN useradd --uid 10001 --create-home agent && mkdir -p /state && chown -R agent:agent /app /state
USER agent
ENV HOST=0.0.0.0 PORT=8080 MODEL_MODE=fixture EMAIL_TRANSPORT=mock ENABLE_REAL_EMAIL=false ENABLE_MCP=false WORKFLOW_DB_PATH=/state/tourism.sqlite
EXPOSE 8080
CMD ["/app/.venv/bin/python", "-m", "tourism_agent"]
