# AYNI – Backend (FastAPI + PostgreSQL/Supabase)

API REST de la plataforma AYNI para la gestión de ollas comunes.

## Requisitos

- [uv](https://docs.astral.sh/uv/) y Python 3.12+
- Proyecto de Supabase (PostgreSQL) con `../sql/001_ayni_postgres.sql` aplicado
- Sin drivers nativos: `psycopg[binary]` + `psycopg_pool`

## Puesta en marcha

1. Ejecute `../sql/001_ayni_postgres.sql` en el SQL Editor de Supabase (crea el esquema `dbo`, tablas, funciones y datos semilla).
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

## Despliegue en Render con Docker (manual)

La imagen se define en `Dockerfile` (Python 3.12 slim + `uv sync --frozen --no-dev`, usuario sin privilegios,
`uvicorn` en `0.0.0.0:$PORT`). `.dockerignore` excluye `.env`, `.venv`, `tests/` y cachés: **los secretos nunca
entran en la imagen**.

### 1 · Probar la imagen en local (Docker Desktop encendido, desde `backend/`)

```powershell
docker build -t ayni-backend .
```

```powershell
docker run --rm -p 8001:8001 -e PORT=8001 --env-file .env ayni-backend
```

Verifique `http://127.0.0.1:8001/api/health` y `http://127.0.0.1:8001/health/db` (debe responder `{"status": "ok"}`).

### 2 · Subir a GitHub

Commit en la rama `backend`, `git push origin backend`, Pull Request `backend → main` y merge con el CI en verde.
Render despliega desde `main`.

### 3 · Crear el Web Service en Render (New → Web Service → repositorio `-AYNI-`)

| Campo | Valor |
|---|---|
| Name | `ayni-backend` → `https://ayni-backend.onrender.com` (si el nombre está ocupado, use la URL que asigne Render) |
| Branch | `main` |
| Language | Docker |
| Root Directory | `backend` |
| Dockerfile Path | `./Dockerfile` |
| Health Check Path | `/api/health` (no use `/health/db`: una caída de Supabase reiniciaría el contenedor en bucle) |
| Instance Type | Free (se suspende tras ~15 min sin tráfico; la primera petición tarda ~50 s) |

Variables de entorno (*Environment*), sin comillas:

| Variable | Valor |
|---|---|
| `DATABASE_URL` | Session pooler de Supabase, puerto **5432**, contraseña URL-encodeada, `?sslmode=require` (la conexión directa `db.<ref>.supabase.co` es solo IPv6 y no funciona desde Render) |
| `JWT_SECRET` | Cadena aleatoria nueva: `uv run python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `JWT_ALGORITHM` | `HS256` |
| `JWT_EXPIRE_MINUTES` | `480` |
| `API_NINJAS_KEY` | Clave de api-ninjas.com |
| `CORS_ORIGINS` | Vacío por ahora (`http://localhost:4200` siempre está permitido). Al desplegar el frontend: su origen sin `/` final |

`PORT` no se define: Render lo inyecta.

### 4 · Verificar

- `https://ayni-backend.onrender.com/api/health`
- `https://ayni-backend.onrender.com/health/db` → `{"status": "ok"}`
- `https://ayni-backend.onrender.com/docs` → login con una cuenta semilla y prueba de un endpoint protegido.

Cuando se despliegue el frontend, su `apiUrl` de producción será `https://ayni-backend.onrender.com/api`
(con `/api` al final) y su origen debe añadirse a `CORS_ORIGINS` en Render.
