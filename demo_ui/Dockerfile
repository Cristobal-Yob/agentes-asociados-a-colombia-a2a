ARG PYTHON_BASE_IMAGE
FROM ${PYTHON_BASE_IMAGE}
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
COPY demo_ui/requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt && python -m pip check
COPY demo_ui/ ./demo_ui/
USER 65532:65532
EXPOSE 8080
CMD ["python", "-B", "-m", "demo_ui"]
