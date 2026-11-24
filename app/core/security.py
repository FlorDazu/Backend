"""Hash de contraseñas (bcrypt), JWT y dependencias de autenticación/roles."""

from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import ExpiredSignatureError, JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings
from app.core.database import consultar_uno, get_db

JWT_SECRET = settings.jwt_secret
JWT_ALGORITHM = settings.jwt_algorithm
JWT_EXPIRE_MINUTES = settings.jwt_expire_minutes

MAX_INTENTOS_FALLIDOS = 5
MINUTOS_BLOQUEO = 15

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer(auto_error=False, description="Token JWT obtenido en /api/auth/login")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verificar_password(password: str, password_hash: str) -> bool:
    try:
        return pwd_context.verify(password, password_hash)
    except (ValueError, TypeError):
        # Hash inválido (por ejemplo 'PENDING_HASH' aún sin actualizar)
        return False


def crear_token(usuario: dict[str, Any]) -> str:
    expira = datetime.now(timezone.utc) + timedelta(minutes=JWT_EXPIRE_MINUTES)
    payload = {
        "sub": str(usuario["id_usuario"]),
        "rol": usuario["rol"],
        "id_olla": usuario.get("id_olla"),
        "exp": expira,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


SQL_USUARIO_ACTUAL = """
SELECT u.id_usuario, u.id_olla, u.nombre_completo, u.username, u.telefono, u.rol,
       u.activo, u.intentos_fallidos, u.bloqueado_hasta, u.fecha_creacion,
       o.codigo_unico, o.nombre AS nombre_olla, o.activada AS olla_activada
FROM dbo.usuarios u
LEFT JOIN dbo.ollas o ON o.id_olla = u.id_olla
WHERE u.id_usuario = ?
"""

_NO_AUTENTICADO = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Debe iniciar sesión para acceder a este recurso.",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_usuario_actual(
    credenciales: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    conn: psycopg.Connection = Depends(get_db),
) -> dict[str, Any]:
    """Valida el JWT y vuelve a verificar en BD que la cuenta y su olla sigan activas."""
    if credenciales is None or not credenciales.credentials:
        raise _NO_AUTENTICADO
    try:
        payload = jwt.decode(credenciales.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        id_usuario = int(payload.get("sub"))
    except ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Su sesión ha expirado. Inicie sesión nuevamente.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except (JWTError, TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="El token de acceso no es válido. Inicie sesión nuevamente.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    usuario = consultar_uno(conn, SQL_USUARIO_ACTUAL, id_usuario)
    if usuario is None:
        raise _NO_AUTENTICADO
    if not usuario["activo"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Su cuenta está desactivada. Comuníquese con el administrador de AYNI.",
        )
    if usuario["id_olla"] is not None and not usuario["olla_activada"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Su olla común está desactivada. Comuníquese con el administrador de AYNI.",
        )
    return usuario


def requiere_admin(usuario: dict[str, Any] = Depends(get_usuario_actual)) -> dict[str, Any]:
    if usuario["rol"] != "ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo el administrador de AYNI puede realizar esta acción.",
        )
    return usuario


def get_id_olla_contexto(
    usuario: dict[str, Any] = Depends(get_usuario_actual),
    id_olla: int | None = Query(
        default=None,
        gt=0,
        description="Solo para ADMIN: olla a consultar/gestionar. Las dirigentas usan siempre su propia olla.",
    ),
    conn: psycopg.Connection = Depends(get_db),
) -> int:
    """Determina sobre qué olla opera la petición.

    - DIRIGENTA: siempre su olla vinculada (se ignora el parámetro).
    - ADMIN: debe indicar `?id_olla=` para auditar/gestionar una olla concreta.
    """
    if usuario["rol"] == "DIRIGENTA" or usuario["id_olla"] is not None:
        if usuario["id_olla"] is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Su cuenta no tiene una olla común asignada.",
            )
        return usuario["id_olla"]
    if id_olla is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Como administrador debe indicar la olla a consultar con el parámetro id_olla.",
        )
    if consultar_uno(conn, "SELECT id_olla FROM dbo.ollas WHERE id_olla = ?", id_olla) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="La olla común indicada no existe.")
    return id_olla
