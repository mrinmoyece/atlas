# Multi-stage, non-root, minimal runtime. The digest makes rebuilds independent
# of mutable Docker Hub tags; Dependabot keeps it current.
FROM python:3.14-slim@sha256:a7fb1e634c4a578f9e0bd6327f11a3cde11b7a9395f48e24360c0988bcc5c2bc AS builder
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY scripts/requirements-runtime.txt ./scripts/requirements-runtime.txt
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install --require-hashes \
        -r scripts/requirements-runtime.txt \
    && pip install --no-cache-dir --prefix=/install --no-deps .

FROM python:3.14-slim@sha256:a7fb1e634c4a578f9e0bd6327f11a3cde11b7a9395f48e24360c0988bcc5c2bc
# Hardening: dedicated unprivileged user and a read-only image filesystem in
# Kubernetes. The audit mount is the sole persistent writable application path.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin atlas \
    && mkdir -p /var/lib/atlas/audit \
    && chown atlas:atlas /var/lib/atlas/audit \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /install /usr/local
# The service never installs packages at runtime. Removing pip avoids shipping
# a package manager and its independent vulnerability surface in the image.
RUN rm -rf /usr/local/lib/python3.12/site-packages/pip \
           /usr/local/lib/python3.12/site-packages/pip-*.dist-info \
           /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12
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
