# Multi-stage, non-root, minimal runtime.
FROM python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install ".[mcp,otel]"

FROM python:3.12-slim
# Hardening: dedicated unprivileged user, no shell, no package manager left
# behind. The container runs read-only in k8s (see k8s/deployment.yaml), so
# nothing writes to the image filesystem at runtime.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin atlas \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /install /usr/local
WORKDIR /app
COPY --chown=atlas:atlas fixtures ./fixtures
COPY --chown=atlas:atlas evals ./evals
USER atlas
EXPOSE 8000
# Explicit roots: `parents[3]` discovery resolves inside site-packages once
# the package is installed, so without these every analysis 404s.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    ATLAS_FIXTURES_ROOT=/app/fixtures/repos \
    ATLAS_PROJECT_ROOT=/app
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/healthz')"
CMD ["uvicorn", "atlas.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
