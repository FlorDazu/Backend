# ============================================================================
# AYNI · Imagen Docker del backend (FastAPI + psycopg + Supabase)
# Construir (desde backend/):  docker build -t ayni-backend .
# Ejecutar en local:           docker run --rm -p 8001:8001 -e PORT=8001 --env-file .env ayni-backend
# En Render, PORT lo define la plataforma automáticamente.
# ============================================================================

# ---------- Etapa 1: instalación de dependencias con uv ----------
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Primero solo los archivos de dependencias para aprovechar la caché de capas.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

# ---------- Etapa 2: imagen final ligera ----------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    PORT=8000

WORKDIR /app

# Usuario sin privilegios para ejecutar la API.
RUN useradd --create-home --uid 10001 ayni

COPY --from=builder /app/.venv /app/.venv
COPY app ./app

USER ayni

EXPOSE 8000

# 0.0.0.0 para aceptar tráfico externo; el puerto sale de $PORT (Render usa 10000).
# Sin --reload: modo producción.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
