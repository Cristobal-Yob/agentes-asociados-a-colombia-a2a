FROM ghcr.io/astral-sh/uv:0.12.13 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY common ./common
COPY email_agent ./email_agent
RUN useradd --uid 10001 --create-home agent && chown -R agent:agent /app
USER agent
ENV HOST=0.0.0.0 PORT=8080 MODEL_MODE=vertex VERTEX_PROJECT_ID=example-project VERTEX_LOCATION=global MODEL_ID=gemini-3.1-flash-lite EMAIL_TRANSPORT=gmail EMAIL_SENDER=student@example.com
EXPOSE 8080
CMD ["/app/.venv/bin/python", "-m", "email_agent"]
