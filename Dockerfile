# syntax=docker/dockerfile:1
# Single-container image: builds the React SPA and serves it from the FastAPI backend
# (API under /api, SPA at every other path). Targets Azure Container Apps.

# ---- Stage 1: build the React SPA --------------------------------------------------
FROM node:22-alpine AS frontend
WORKDIR /web
# Install exactly the audited lockfile before copying sources, so a dependency change is the
# only thing that invalidates the npm layer.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# Same-origin API base: the bundle calls /api/... on whatever host serves it.
ENV VITE_API_BASE=/api
ARG APP_VERSION=dev
ENV VITE_APP_VERSION=$APP_VERSION
RUN npm run build

# ---- Stage 2: backend + bundled SPA ------------------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION

# Pick up the latest Debian security patches for the base image rather than inheriting
# whatever was current when the tag was published.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Backend source must be present before the install: setuptools validates the package dir.
COPY backend/ ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && pip install --no-deps .

# Bundled SPA goes into the package's static dir, which main.py serves.
COPY --from=frontend /web/dist ./app/static

# Writable state (SQLite when no DATABASE_URL, the Fernet key, connection and connector
# registries). In Azure this path is an Azure Files mount so it survives image rolls.
ENV DATA_DIR=/app/.data
RUN mkdir -p /app/.data

# An image is a deployment artefact, so it defaults to the hardened posture: no /docs, /redoc or
# /openapi.json, and Secure session cookies. A local checkout running uvicorn from source still
# defaults to development, and docker-compose sets it back explicitly.
ENV ENVIRONMENT=production

# Drop privileges: nothing here needs root at runtime.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# create_all + the dialect-guarded schema steps run in the app's own startup, so the container
# needs no separate migration step.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8000"]
