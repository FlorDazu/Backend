"""AYNI - API REST para la gestión de Ollas Comunes.

Ejecutar (sin --reload):
    uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
"""

import logging
from contextlib import asynccontextmanager
from typing import Any

import psycopg
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.apis import admin, auth, donaciones, inventario, olla, public, recetas, reportes
from app.core.config import settings
from app.core.database import close_pool, consultar, consultar_uno, ejecutar, obtener_conexion, traducir_error_sql
from app.core.security import hash_password

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("ayni")

PASSWORDS_SEMILLA = {
    "admin@ayni.org": "Admin1234!",
    "dirigenta@ayni.org": "Dirigenta1234!",
    "maria.condori@ayni.org": "Dirigenta1234!",
}


def actualizar_hashes_pendientes() -> None:
    """Reemplaza password_hash = 'PENDING_HASH' por hashes bcrypt reales."""
    conn = obtener_conexion()
    try:
        pendientes = consultar(
            conn, "SELECT id_usuario, username, rol FROM dbo.usuarios WHERE password_hash = 'PENDING_HASH'"
        )
        for usuario in pendientes:
            username = usuario["username"].lower()
            clave = PASSWORDS_SEMILLA.get(username, "Admin1234!" if usuario["rol"] == "ADMIN" else "Dirigenta1234!")
            ejecutar(
                conn,
                "UPDATE dbo.usuarios SET password_hash = ? WHERE id_usuario = ? AND password_hash = 'PENDING_HASH'",
                hash_password(clave),
                usuario["id_usuario"],
            )
            logger.info("Contraseña semilla inicializada para %s", username)
        if not pendientes:
            logger.info("Conexión a PostgreSQL correcta. No hay contraseñas pendientes por inicializar.")
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    for aviso in settings.advertencias():
        logger.warning("Configuración: %s", aviso)
    try:
        actualizar_hashes_pendientes()
    except Exception as exc:  # noqa: BLE001 - la API debe arrancar aunque la BD no esté lista
        logger.warning(
            "No se pudieron inicializar las contraseñas semilla (PostgreSQL no disponible o script pendiente). "
            "La API seguirá funcionando. Detalle: %s",
            exc,
        )
    yield
    close_pool()


app = FastAPI(
    title="AYNI API - Ollas Comunes",
    description="Backend de la plataforma AYNI: portal público, panel de dirigentas, inventario PEPS, "
    "recetas, reportes y administración.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)


# ---------------------------------------------------------------------------
# Manejo de errores en español (HU-05 / HU-13): siempre {"detail": "..."}
# ---------------------------------------------------------------------------

def _campo(loc: tuple[Any, ...]) -> str:
    partes = [str(p) for p in loc if p not in ("body", "query", "path", "header")]
    if not partes:
        return ""
    nombre = partes[0].replace("_", " ")
    if len(partes) > 1:
        detalle = " → ".join(
            f"ítem {int(p) + 1}" if p.isdigit() else p.replace("_", " ") for p in partes[1:]
        )
        nombre = f"{nombre} ({detalle})"
    return nombre


def _traducir_error(err: dict[str, Any]) -> str:
    tipo = err.get("type", "")
    ctx = err.get("ctx") or {}
    campo = _campo(tuple(err.get("loc", ())))
    etiqueta = f"El campo «{campo}»" if campo else "El dato enviado"

    if tipo == "value_error":
        mensaje = str(err.get("msg", ""))
        return mensaje.removeprefix("Value error, ").strip() or f"{etiqueta} no es válido."
    if tipo == "assertion_error":
        return str(err.get("msg", "")).removeprefix("Assertion failed, ").strip()

    plantillas = {
        "missing": f"{etiqueta} es obligatorio.",
        "string_too_short": f"{etiqueta} debe tener al menos {ctx.get('min_length')} caracteres.",
        "string_too_long": f"{etiqueta} debe tener como máximo {ctx.get('max_length')} caracteres.",
        "string_type": f"{etiqueta} debe ser un texto.",
        "string_pattern_mismatch": f"{etiqueta} tiene un formato no válido.",
        "greater_than": f"{etiqueta} debe ser mayor que {ctx.get('gt')}.",
        "greater_than_equal": f"{etiqueta} debe ser mayor o igual que {ctx.get('ge')}.",
        "less_than": f"{etiqueta} debe ser menor que {ctx.get('lt')}.",
        "less_than_equal": f"{etiqueta} debe ser menor o igual que {ctx.get('le')}.",
        "int_parsing": f"{etiqueta} debe ser un número entero.",
        "int_type": f"{etiqueta} debe ser un número entero.",
        "int_from_float": f"{etiqueta} debe ser un número entero (sin decimales).",
        "float_parsing": f"{etiqueta} debe ser un número válido.",
        "float_type": f"{etiqueta} debe ser un número válido.",
        "decimal_parsing": f"{etiqueta} debe ser un número válido.",
        "decimal_type": f"{etiqueta} debe ser un número válido.",
        "decimal_max_places": f"{etiqueta} admite como máximo {ctx.get('decimal_places')} decimales.",
        "decimal_max_digits": f"{etiqueta} tiene demasiados dígitos.",
        "bool_parsing": f"{etiqueta} debe ser verdadero o falso.",
        "bool_type": f"{etiqueta} debe ser verdadero o falso.",
        "date_parsing": f"{etiqueta} debe ser una fecha válida con formato AAAA-MM-DD.",
        "date_from_datetime_parsing": f"{etiqueta} debe ser una fecha válida con formato AAAA-MM-DD.",
        "date_type": f"{etiqueta} debe ser una fecha válida con formato AAAA-MM-DD.",
        "literal_error": f"{etiqueta} debe ser uno de los siguientes valores: {ctx.get('expected')}.",
        "enum": f"{etiqueta} debe ser uno de los siguientes valores: {ctx.get('expected')}.",
        "list_type": f"{etiqueta} debe ser una lista.",
        "too_long": f"{etiqueta} tiene demasiados elementos (máximo {ctx.get('max_length')}).",
        "model_type": "El cuerpo de la solicitud debe ser un objeto JSON.",
        "dict_type": "El cuerpo de la solicitud debe ser un objeto JSON.",
        "json_invalid": "El cuerpo de la solicitud no es un JSON válido.",
        "extra_forbidden": f"{etiqueta} no está permitido.",
    }
    return plantillas.get(tipo, f"{etiqueta} tiene un valor no válido.")


@app.exception_handler(RequestValidationError)
async def manejar_validacion(_: Request, exc: RequestValidationError) -> JSONResponse:
    errores = []
    for err in exc.errors():
        errores.append({"campo": _campo(tuple(err.get("loc", ()))) or None, "mensaje": _traducir_error(err)})
    mensajes = list(dict.fromkeys(e["mensaje"] for e in errores))
    return JSONResponse(
        status_code=422,
        content={"detail": " ".join(mensajes) or "Los datos enviados no son válidos.", "errores": errores},
    )


_TRADUCCIONES_HTTP = {
    "Not Found": "El recurso solicitado no existe.",
    "Method Not Allowed": "Método HTTP no permitido para este recurso.",
    "Not authenticated": "Debe iniciar sesión para acceder a este recurso.",
    "Internal Server Error": "Ocurrió un error interno. Intente nuevamente.",
}


@app.exception_handler(StarletteHTTPException)
async def manejar_http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    detalle = exc.detail
    if isinstance(detalle, str):
        detalle = _TRADUCCIONES_HTTP.get(detalle, detalle)
    return JSONResponse(status_code=exc.status_code, content={"detail": detalle}, headers=getattr(exc, "headers", None))


@app.exception_handler(psycopg.Error)
async def manejar_psycopg(_: Request, exc: psycopg.Error) -> JSONResponse:
    http_exc = traducir_error_sql(exc)
    return JSONResponse(status_code=http_exc.status_code, content={"detail": http_exc.detail})


@app.exception_handler(Exception)
async def manejar_generico(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Error no controlado: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Ocurrió un error inesperado en el servidor. Intente nuevamente en unos minutos."},
    )


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(public.router)
app.include_router(auth.router)
app.include_router(olla.router)
app.include_router(donaciones.router)
app.include_router(inventario.router)
app.include_router(recetas.router)
app.include_router(reportes.router)
app.include_router(admin.router)


@app.get("/", tags=["Sistema"], summary="Información de la API")
def raiz() -> dict[str, str]:
    return {"mensaje": "API AYNI operativa. Documentación disponible en /docs", "version": app.version}


@app.get("/api/health", tags=["Sistema"], summary="Estado de la API y de la base de datos")
def health() -> dict[str, Any]:
    try:
        conn = obtener_conexion()
        try:
            consultar_uno(conn, "SELECT 1 AS ok")
        finally:
            conn.close()
        return {"api": "ok", "base_datos": "ok"}
    except psycopg.Error as exc:
        return {"api": "ok", "base_datos": "sin conexión", "detalle": traducir_error_sql(exc).detail}
    except Exception as exc:
        return {"api": "ok", "base_datos": "sin conexión", "detalle": str(exc)}


@app.get("/health/db", tags=["Sistema"], summary="Comprobación de salud de la base de datos PostgreSQL")
def health_db() -> dict[str, str]:
    if not settings.database_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DATABASE_URL no configurada en las variables de entorno.",
        )
    try:
        conn = obtener_conexion()
        try:
            consultar_uno(conn, "SELECT 1 AS ok")
        finally:
            conn.close()
        return {"status": "ok"}
    except psycopg.OperationalError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No se pudo establecer conexión con PostgreSQL.",
        )
    except Exception as exc:
        logger.warning("Error en comprobación /health/db: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Servicio de base de datos no disponible.",
        )
