# The pages' stylesheets, built from the classes the templates use (tailwind/).
# Served from our own /static, so no page depends on a CDN for its styles.
FROM node:20-alpine AS css
WORKDIR /build/tailwind
COPY tailwind/package.json tailwind/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY tailwind ./
COPY app/templates /build/app/templates
COPY app/static /build/app/static
RUN npm run build

# Python 3.12: every C-extension dependency (asyncpg, pydantic-core) ships a
# prebuilt wheel for it. 3.13/3.14 currently force a source build.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so the layer caches across code changes.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app ./app
# Freshly built from this commit's templates (replaces the committed copies).
COPY --from=css /build/app/static/css ./app/static/css
COPY alembic ./alembic
COPY alembic.ini .
COPY scripts ./scripts

# Run as a non-root user.
RUN useradd --create-home --uid 10001 shero && chown -R shero:shero /app
USER shero

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
