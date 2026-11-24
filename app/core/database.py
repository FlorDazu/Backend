"""Conexión a PostgreSQL (Supabase / local) mediante psycopg y psycopg_pool."""

import logging
import re
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from fastapi import HTTPException, status
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.core.config import settings

logger = logging.getLogger("ayni.database")

_pool: ConnectionPool | None = None


def obtener_cadena_conexion() -> str:
    """Devuelve la URL de conexión definida en app/core/config.py (DATABASE_URL o DB_*)."""
    return settings.dsn


def get_pool() -> ConnectionPool:
    """Obtiene o crea el pool de conexiones de psycopg_pool."""
    global _pool
    if _pool is None or _pool.closed:
        dsn = obtener_cadena_conexion()
        _pool = ConnectionPool(
            conninfo=dsn,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            timeout=settings.db_timeout,
            open=True,
            kwargs={"row_factory": dict_row, "autocommit": True},
        )
    return _pool


def close_pool() -> None:
    """Cierra el pool de conexiones al apagar la aplicación."""
    global _pool
    if _pool is not None and not _pool.closed:
        try:
            _pool.close()
            logger.info("Pool de conexiones PostgreSQL cerrado correctamente.")
        except Exception as exc:
            logger.warning("Error al cerrar pool de conexiones: %s", exc)
        finally:
            _pool = None


class PooledConnectionWrapper:
    """Envuelve una conexión obtenida del pool para devolverla al pool con .close()."""

    def __init__(self, pool: ConnectionPool, conn: psycopg.Connection):
        self._pool = pool
        self._conn: psycopg.Connection | None = conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._pool.putconn(self._conn)
            except Exception:
                pass
            self._conn = None

    def __getattr__(self, name: str) -> Any:
        if self._conn is None:
            raise RuntimeError("La conexión ya ha sido cerrada.")
        return getattr(self._conn, name)

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        if self._conn is None:
            raise RuntimeError("La conexión ya ha sido cerrada.")
        return self._conn.cursor(*args, **kwargs)

    def transaction(self) -> Any:
        if self._conn is None:
            raise RuntimeError("La conexión ya ha sido cerrada.")
        return self._conn.transaction()

    def __enter__(self) -> "PooledConnectionWrapper":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


def obtener_conexion() -> Any:
    """Abre una conexión administrada mediante el pool de conexiones."""
    pool = get_pool()
    conn = pool.getconn()
    return PooledConnectionWrapper(pool, conn)


def get_db() -> Iterator[Any]:
    """Dependencia de FastAPI: entrega una conexión y la libera al terminar."""
    conn = obtener_conexion()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaccion(conn: Any) -> Iterator[Any]:
    """Agrupa varias sentencias en una transacción explícita (todo o nada)."""
    with conn.transaction():
        cursor = conn.cursor()
        try:
            yield cursor
        finally:
            cursor.close()


# ---------------------------------------------------------------------------
# Adaptador de sintaxis heredada en los routers (EXEC/OUTPUT/ISNULL/? → PostgreSQL)
# ---------------------------------------------------------------------------

_RE_EXEC = re.compile(r"^\s*EXEC\s+(?:dbo\.)?(\w+)\s*(.*)$", re.IGNORECASE | re.DOTALL)
_RE_OUTPUT = re.compile(r"OUTPUT\s+inserted\.(\w+)", re.IGNORECASE)
_RE_BOOLEANOS = re.compile(
    r"\b(activada|atiende_hoy|recibe_frescos|activo|activa|verificada|es_frecuente|requiere_vencimiento)\s*([=<>]+)\s*([01])\b",
    re.IGNORECASE,
)


def _nombre_funcion_pg(nombre: str) -> str:
    """sp_RegistrarSalidaPEPS -> sp_registrar_salida_peps (nombres de sql/001_ayni_postgres.sql)."""
    snake = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", nombre)
    return re.sub(r"_+", "_", snake).lower()


def _reemplazar_booleano(match: re.Match) -> str:
    col = match.group(1)
    op = match.group(2)
    val = match.group(3)
    es_true = (val == "1" and op == "=") or (val == "0" and op in ("<>", "!="))
    return f"{col} = {'TRUE' if es_true else 'FALSE'}"


def _adaptar_sql_y_params(sql: str, params: tuple[Any, ...]) -> tuple[str, list[Any]]:
    """Convierte la sintaxis heredada de los routers (EXEC, OUTPUT, ISNULL, ?) a PostgreSQL."""
    sql_mod = sql.strip()

    # 1. Transformar EXEC dbo.sp_Nombre @param1 = ?, @param2 = ? -> SELECT * FROM dbo.sp_nombre(...)
    match_exec = _RE_EXEC.match(sql_mod)
    if match_exec:
        sp_name = _nombre_funcion_pg(match_exec.group(1))
        args_str = match_exec.group(2).strip()
        if not args_str:
            sql_mod = f"SELECT * FROM dbo.{sp_name}();"
        else:
            partes = [p.strip() for p in args_str.split(",") if p.strip()]
            call_args = []
            for parte in partes:
                val = parte.split("=", 1)[1].strip() if "=" in parte else parte.strip()
                val_upper = val.upper()
                if val_upper == "NULL":
                    call_args.append("NULL")
                elif val == "0":
                    call_args.append("FALSE")
                elif val == "1":
                    call_args.append("TRUE")
                elif val == "?":
                    call_args.append("%s")
                else:
                    call_args.append(val)
            sql_mod = f"SELECT * FROM dbo.{sp_name}({', '.join(call_args)});"

    # 2. Transformar OUTPUT inserted.col -> RETURNING col
    match_output = _RE_OUTPUT.search(sql_mod)
    if match_output:
        col = match_output.group(1)
        sql_mod = re.sub(r"OUTPUT\s+inserted\.\w+", "", sql_mod, flags=re.IGNORECASE)
        sql_mod = f"{sql_mod.rstrip().rstrip(';')} RETURNING {col};"

    # 3. Funciones heredadas -> equivalentes PostgreSQL
    sql_mod = re.sub(r"\bISNULL\b", "COALESCE", sql_mod, flags=re.IGNORECASE)
    sql_mod = re.sub(r"\bGETDATE\(\)", "CURRENT_TIMESTAMP", sql_mod, flags=re.IGNORECASE)
    sql_mod = re.sub(r"\bSYSDATETIME\(\)", "CURRENT_TIMESTAMP", sql_mod, flags=re.IGNORECASE)

    # 4. Ajustar operadores booleanos enteros (activada = 1 -> activada = TRUE)
    sql_mod = _RE_BOOLEANOS.sub(_reemplazar_booleano, sql_mod)

    # 5. Marcadores de posición ? -> %s
    sql_mod = sql_mod.replace("?", "%s")

    return sql_mod, list(params)


# ---------------------------------------------------------------------------
# Helpers de ejecución y consulta
# ---------------------------------------------------------------------------

def ejecutar_en_cursor(cursor: Any, sql: str, *params: Any) -> list[dict[str, Any]]:
    """Ejecuta una consulta sobre un cursor existente y devuelve las filas como dict."""
    sql_adaptada, params_adaptados = _adaptar_sql_y_params(sql, params)
    cursor.execute(sql_adaptada, params_adaptados)
    if cursor.description is not None:
        return list(cursor.fetchall())
    return []


def consultar(conn: Any, sql: str, *params: Any) -> list[dict[str, Any]]:
    """Ejecuta una consulta SELECT y devuelve una lista de diccionarios."""
    cursor = conn.cursor()
    try:
        return ejecutar_en_cursor(cursor, sql, *params)
    finally:
        cursor.close()


def consultar_uno(conn: Any, sql: str, *params: Any) -> dict[str, Any] | None:
    """Devuelve la primera fila como diccionario o None si no hay resultados."""
    filas = consultar(conn, sql, *params)
    return filas[0] if filas else None


def ejecutar_sp(conn: Any, sql: str, *params: Any) -> dict[str, Any]:
    """Ejecuta un procedimiento/función almacenada y devuelve la primera fila."""
    return consultar_uno(conn, sql, *params) or {}


def ejecutar(conn: Any, sql: str, *params: Any) -> int:
    """Ejecuta sentencias UPDATE/DELETE/INSERT y devuelve el número de filas afectadas."""
    cursor = conn.cursor()
    try:
        sql_adaptada, params_adaptados = _adaptar_sql_y_params(sql, params)
        cursor.execute(sql_adaptada, params_adaptados)
        return cursor.rowcount
    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# Traducción de errores de PostgreSQL a mensajes HTTP claros en español
# ---------------------------------------------------------------------------

_RE_NEGOCIO = re.compile(r"(?P<msg>.+?)\s*\((?P<num>5\d{4})\)")
_CODIGOS_CONEXION_PG = {"08000", "08003", "08006", "08001", "08004", "08007", "08P01", "57P01", "57P02", "57P03"}


def traducir_error_sql(error: Exception) -> HTTPException:
    """Convierte un error de psycopg / PostgreSQL en una HTTPException en español."""
    texto_error = str(error)

    # Errores de lógica de negocio (THROW / RAISE EXCEPTION con código 5xxxx)
    coincidencia = _RE_NEGOCIO.search(texto_error)
    if coincidencia:
        numero = int(coincidencia.group("num"))
        mensaje = coincidencia.group("msg").strip()
        codigo = status.HTTP_409_CONFLICT if numero in (50001, 50010) else status.HTTP_400_BAD_REQUEST
        return HTTPException(status_code=codigo, detail=mensaje)

    sqlstate = getattr(error, "sqlstate", "") or ""

    if isinstance(error, psycopg.OperationalError) or sqlstate in _CODIGOS_CONEXION_PG:
        logger.error("Error de conexión a PostgreSQL: %s", texto_error)
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No se pudo conectar con la base de datos PostgreSQL. Verifique la conexión en .env.",
        )

    # 23505: unique_violation
    if sqlstate == "23505":
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya existe un registro con esos datos. Revise que no esté duplicado.",
        )

    # 23503: foreign_key_violation
    if sqlstate == "23503":
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Alguno de los datos enviados hace referencia a un registro que no existe o no cumple las reglas de la base de datos.",
        )

    # 23502 (not_null_violation) o 23514 (check_violation)
    if sqlstate in ("23502", "23514", "22001"):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Falta un dato obligatorio o algún texto supera la longitud permitida.",
        )

    logger.error("Error de base de datos no controlado: %s", texto_error, exc_info=error)
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Ocurrió un error interno en la base de datos. Intente nuevamente en unos minutos.",
    )
