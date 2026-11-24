"""HU-06 / HU-07: Registro de dirigenta en 1 paso, login con bloqueo temporal y perfil."""

from typing import Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, status

from app.core.database import consultar_uno, ejecutar, ejecutar_sp, get_db
from app.schemas import LoginRequest, RegistroOllaCuenta
from app.core.security import (
    MAX_INTENTOS_FALLIDOS,
    MINUTOS_BLOQUEO,
    SQL_USUARIO_ACTUAL,
    crear_token,
    get_usuario_actual,
    hash_password,
    verificar_password,
)

router = APIRouter(prefix="/api/auth", tags=["Autenticación"])

SQL_OLLA_RESUMEN = """
SELECT o.id_olla, o.codigo_unico, o.nombre, o.direccion, o.referencia, o.telefono,
       o.beneficiarios, o.atiende_hoy, o.recibe_frescos, o.necesidades_urgentes,
       o.activada, o.verificada, o.fecha_registro,
       u.id_ubigeo, u.departamento, u.provincia, u.distrito
FROM dbo.ollas o
JOIN dbo.ubigeos u ON u.id_ubigeo = o.id_ubigeo
WHERE o.id_olla = ?
"""


def _perfil(usuario: dict[str, Any]) -> dict[str, Any]:
    return {
        "id_usuario": usuario["id_usuario"],
        "nombre_completo": usuario["nombre_completo"],
        "username": usuario["username"],
        "telefono": usuario["telefono"],
        "rol": usuario["rol"],
        "id_olla": usuario["id_olla"],
    }


def _respuesta_sesion(conn: psycopg.Connection, usuario: dict[str, Any], mensaje: str) -> dict[str, Any]:
    olla = consultar_uno(conn, SQL_OLLA_RESUMEN, usuario["id_olla"]) if usuario["id_olla"] else None
    return {
        "mensaje": mensaje,
        "access_token": crear_token(usuario),
        "token_type": "bearer",
        "rol": usuario["rol"],
        "usuario": _perfil(usuario),
        "olla": olla,
    }


@router.post(
    "/registro-olla",
    status_code=status.HTTP_201_CREATED,
    summary="Crear cuenta de dirigenta + olla común en un solo paso",
)
@router.post(
    "/activar",
    status_code=status.HTTP_201_CREATED,
    summary="Alias de /registro-olla",
    include_in_schema=True,
)
def registrar_olla_y_cuenta(datos: RegistroOllaCuenta, conn: psycopg.Connection = Depends(get_db)):
    creado = ejecutar_sp(
        conn,
        """EXEC dbo.sp_CrearCuentaDirigenta
            @nombre_completo = ?, @username = ?, @telefono = ?, @password_hash = ?,
            @nombre_olla = ?, @id_ubigeo = ?, @direccion = ?, @referencia = ?""",
        datos.nombre_completo,
        datos.username,
        datos.telefono,
        hash_password(datos.password),
        datos.nombre_olla,
        datos.id_ubigeo,
        datos.direccion,
        datos.referencia,
    )
    if not creado.get("id_usuario"):
        raise HTTPException(status_code=500, detail="No se pudo completar el registro. Intente nuevamente.")

    usuario = consultar_uno(conn, SQL_USUARIO_ACTUAL, creado["id_usuario"])
    return _respuesta_sesion(
        conn,
        usuario,
        f"¡Bienvenida a AYNI! Su cuenta y la olla {creado['codigo_unico']} fueron creadas y ya están activas.",
    )


@router.post("/login", summary="Iniciar sesión (usuario o correo + contraseña)")
def login(datos: LoginRequest, conn: psycopg.Connection = Depends(get_db)):
    credencial = consultar_uno(
        conn,
        """SELECT id_usuario, password_hash, intentos_fallidos, bloqueado_hasta,
                  CASE WHEN bloqueado_hasta > CURRENT_TIMESTAMP
                       THEN CEIL(EXTRACT(EPOCH FROM (bloqueado_hasta - CURRENT_TIMESTAMP)))::INT ELSE 0 END AS segundos_bloqueo
           FROM dbo.usuarios WHERE username = ?""",
        datos.username,
    )
    error_credenciales = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuario o contraseña incorrectos."
    )
    if credencial is None:
        raise error_credenciales

    if credencial["segundos_bloqueo"] > 0:
        minutos = max(1, -(-credencial["segundos_bloqueo"] // 60))
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=f"Su cuenta está bloqueada temporalmente por intentos fallidos. Intente nuevamente en {minutos} minuto(s).",
        )

    if credencial["bloqueado_hasta"] is not None:
        # El bloqueo anterior ya venció: se reinicia el contador.
        ejecutar(
            conn,
            "UPDATE dbo.usuarios SET intentos_fallidos = 0, bloqueado_hasta = NULL WHERE id_usuario = ?",
            credencial["id_usuario"],
        )

    if not verificar_password(datos.password, credencial["password_hash"]):
        intentos = ejecutar_sp(
            conn,
            """UPDATE dbo.usuarios
               SET intentos_fallidos = intentos_fallidos + 1,
                   bloqueado_hasta = CASE WHEN intentos_fallidos + 1 >= ?
                                          THEN CURRENT_TIMESTAMP + make_interval(mins => ?::INT) ELSE NULL END
               OUTPUT inserted.intentos_fallidos
               WHERE id_usuario = ?""",
            MAX_INTENTOS_FALLIDOS,
            MINUTOS_BLOQUEO,
            credencial["id_usuario"],
        )["intentos_fallidos"]
        if intentos >= MAX_INTENTOS_FALLIDOS:
            raise HTTPException(
                status_code=status.HTTP_423_LOCKED,
                detail=f"Ha superado {MAX_INTENTOS_FALLIDOS} intentos fallidos. Su cuenta quedó bloqueada por {MINUTOS_BLOQUEO} minutos.",
            )
        restantes = MAX_INTENTOS_FALLIDOS - intentos
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Usuario o contraseña incorrectos. Le quedan {restantes} intento(s) antes del bloqueo temporal.",
        )

    usuario = consultar_uno(conn, SQL_USUARIO_ACTUAL, credencial["id_usuario"])
    if not usuario["activo"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Su cuenta está desactivada. Comuníquese con el administrador de AYNI.",
        )
    if usuario["id_olla"] is not None and not usuario["olla_activada"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Su olla común está desactivada. Comuníquese con el administrador de AYNI para reactivarla.",
        )

    ejecutar(
        conn,
        "UPDATE dbo.usuarios SET intentos_fallidos = 0, bloqueado_hasta = NULL WHERE id_usuario = ?",
        usuario["id_usuario"],
    )
    return _respuesta_sesion(conn, usuario, f"Bienvenida(o), {usuario['nombre_completo']}.")


@router.get("/me", summary="Perfil del usuario autenticado")
def me(usuario: dict = Depends(get_usuario_actual), conn: psycopg.Connection = Depends(get_db)):
    olla = consultar_uno(conn, SQL_OLLA_RESUMEN, usuario["id_olla"]) if usuario["id_olla"] else None
    return {**_perfil(usuario), "activo": bool(usuario["activo"]), "fecha_creacion": usuario["fecha_creacion"], "olla": olla}
