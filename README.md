# AYNI – Backend (FastAPI + PostgreSQL/Supabase)

API REST de la plataforma AYNI para la gestión de ollas comunes.

## Requisitos

- [uv](https://docs.astral.sh/uv/) y Python 3.12+
- Proyecto de Supabase (PostgreSQL) con el esquema `sql/001_ayni_postgres.sql` aplicado (vive en el repositorio principal `FlorDazu/-AYNI-`)
- Sin drivers nativos: `psycopg[binary]` + `psycopg_pool`

## Puesta en marcha

1. Ejecute `sql/001_ayni_postgres.sql` (repositorio `-AYNI-`) en el SQL Editor de Supabase (crea el esquema `dbo`, tablas, funciones y datos semilla).
2. Copie `.env.example` a `.env` y complete `DATABASE_URL` (Session pooler, contraseña URL-encodeada), `JWT_SECRET` y `API_NINJAS_KEY`. Toda la configuración se lee en `app/core/config.py`.
3. Instale dependencias y levante la API (sin `--reload`):

```powershell
uv sync
```

```powershell
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Documentación interactiva: http://127.0.0.1:8000/docs · Estado: http://127.0.0.1:8000/api/health · BD: http://127.0.0.1:8000/health/db

Al arrancar, la API reemplaza los `password_hash = 'PENDING_HASH'` por hashes bcrypt:

| Usuario | Contraseña | Rol |
|---|---|---|
| admin@ayni.org | Admin1234! | ADMIN |
| dirigenta@ayni.org | Dirigenta1234! | DIRIGENTA |
| maria.condori@ayni.org | Dirigenta1234! | DIRIGENTA |

## Endpoints por Historia de Usuario

| HU | Endpoint(s) |
|---|---|
| HU-01 | `GET /api/public/kpis` |
| HU-02 | `GET /api/public/ubigeos`, `GET /api/public/ollas` |
| HU-03 | `GET /api/public/ollas`, `GET /api/public/ollas/{id_olla}` |
| HU-04 | `POST /api/public/ollas` (sin cuenta) · `POST /api/auth/registro-olla` (con cuenta) |
| HU-05 | Validaciones y errores en español en toda la API (`{"detail": "..."}`) |
| HU-06 | `POST /api/auth/registro-olla` · alias `POST /api/auth/activar` |
| HU-07 | `POST /api/auth/login`, `GET /api/auth/me` (bloqueo 15 min tras 5 intentos) |
| HU-08 | `GET /api/olla/mi-olla`, `PUT /api/olla/estado-diario` |
| HU-09 | `GET /api/donantes`, `POST /api/donantes` |
| HU-10 | `GET /api/donaciones`, `POST /api/donaciones` |
| HU-11 | `GET /api/insumos`, `GET /api/inventario/stock`, `GET /api/inventario/lotes`, `POST /api/inventario/entrada`, `POST /api/inventario/salida`, `POST /api/inventario/descartar` |
| HU-12 | `GET /api/recetas?raciones=`, `POST /api/recetas/despachar`, `GET /api/recetas/externas?q=` |
| HU-13 | Toda respuesta exitosa de `POST/PUT/PATCH` incluye `mensaje` |
| HU-14 | `GET /api/movimientos`, `GET /api/reportes/resumen`, `GET /api/reportes/exportar-csv`, `/api/admin/*` |

Las rutas con datos de una olla usan la olla de la dirigenta autenticada. Un `ADMIN` puede consultarlas
indicando `?id_olla=N`.

## Despliegue en Render con Docker

- **URL pública:** <https://backend-mqef.onrender.com> · Swagger: `/docs` · Salud: `/api/health` · Base de datos: `/health/db`
- La imagen se define en `Dockerfile` (Python 3.12 slim + `uv sync --frozen --no-dev`, usuario sin privilegios,
  `uvicorn` en `0.0.0.0:$PORT`). `.dockerignore` excluye `.env`, `.venv`, `tests/` y cachés: **los secretos nunca
  entran en la imagen**.

### Probar la imagen en local (Docker Desktop encendido, desde la raíz del repo)

```powershell
docker build -t ayni-backend .
```

```powershell
docker run --rm -p 8001:8001 -e PORT=8001 --env-file .env ayni-backend
```

Verifique `http://127.0.0.1:8001/api/health` y `http://127.0.0.1:8001/health/db` (debe responder `{"status": "ok"}`).

### Configuración del Web Service en Render

| Campo | Valor |
|---|---|
| Repositorio / rama | `FlorDazu/Backend` / `main` |
| Language | Docker |
| Root Directory | *(vacío: el `Dockerfile` está en la raíz)* |
| Dockerfile Path | `./Dockerfile` |
| Health Check Path | `/api/health` (no use `/health/db`: una caída de Supabase reiniciaría el contenedor en bucle) |
| Auto-Deploy | **Off** (el despliegue lo dispara GitHub Actions, ver sección CI/CD) |
| Instance Type | Free (se suspende tras ~15 min sin tráfico; la primera petición tarda ~50 s) |

Variables de entorno (*Environment*), sin comillas:

| Variable | Valor |
|---|---|
| `DATABASE_URL` | Session pooler de Supabase, puerto **5432**, contraseña URL-encodeada, `?sslmode=require` (la conexión directa `db.<ref>.supabase.co` es solo IPv6 y no funciona desde Render) |
| `JWT_SECRET` | Cadena aleatoria nueva: `uv run python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `JWT_ALGORITHM` | `HS256` |
| `JWT_EXPIRE_MINUTES` | `480` |
| `API_NINJAS_KEY` | Clave de api-ninjas.com |
| `CORS_ORIGINS` | Origen del frontend desplegado, sin `/` final (p. ej. `https://ayni-frontend.onrender.com`). `http://localhost:4200` siempre está permitido |

`PORT` no se define: Render lo inyecta.

## CI/CD con GitHub Actions

Dos workflows en `.github/workflows/`:

**`ci.yml` — CI** (en cada `push` y `pull_request` hacia `main`)

| Job | Qué ejecuta |
|---|---|
| `test` | `astral-sh/setup-uv` (0.13.1) → `uv sync --frozen` → carga de la app → `uv run pytest` |
| `docker` (`needs: test`) | `docker/setup-buildx-action` → `docker/build-push-action` con `push: false` y caché `type=gha` (solo valida que la imagen construye; Render la construye al recibir el hook) → arranca el contenedor y comprueba `GET /api/health` sin base de datos ni secretos |

**`deploy.yml` — Deploy** (encadenado con `workflow_run` al workflow `CI`)

- Solo corre si el run de `CI` terminó en **verde** y venía de un **push a `main`** (los PR nunca despliegan), o si se lanza a mano con `workflow_dispatch`.
- Usa el entorno `production` (`https://backend-mqef.onrender.com`) y `concurrency: deploy-production` (los despliegues se encolan, nunca en paralelo).
- Pasos: `curl -fsS -X POST` al Deploy Hook de Render → espera de 3 min → **smoke test** de `/api/health` y `/health/db` (hasta 20 reintentos cada 30 s; el job falla si no responden `200`).

**Secreto requerido:** `HOOK_RENDER_BACKEND` (*Settings → Secrets and variables → Actions*), con la **Deploy Hook URL**
del servicio (Render → *Settings → Deploy Hook*). Solo se usa en el job de despliegue y GitHub lo oculta en los logs.

> ⚠️ **Desactive Auto-Deploy en Render** (*Settings → Build & Deploy → Auto-Deploy: Off*). Si queda activo, cada push
> a `main` despliega **dos veces** (una por Render y otra por el workflow) y Render publicaría incluso commits con
> pruebas en rojo.

Si el smoke test de `/health/db` falla con `503`, revise que el proyecto de Supabase no esté pausado y que
`DATABASE_URL` en Render sea correcta.
