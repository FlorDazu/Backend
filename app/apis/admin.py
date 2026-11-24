"""HU-14: Gestión total del administrador (ollas y usuarios). Solo rol ADMIN."""

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.database import consultar, consultar_uno, ejecutar, get_db
from app.schemas import OllaEstadoAdmin, UsuarioAdminUpdate
from app.core.security import requiere_admin

router = APIRouter(prefix="/api/admin", tags=["Administración"], dependencies=[Depends(requiere_admin)])

SQL_OLLAS_ADMIN = """
SELECT o.id_olla, o.codigo_unico, o.nombre, o.direccion, o.referencia, o.telefono,
       o.beneficiarios, o.atiende_hoy, o.recibe_frescos, o.necesidades_urgentes,
       o.activada, o.verificada, o.fecha_registro,
       u.id_ubigeo, u.departamento, u.provincia, u.distrito,
       (SELECT COUNT(*) FROM dbo.usuarios x WHERE x.id_olla = o.id_olla) AS total_usuarios
FROM dbo.ollas o
JOIN dbo.ubigeos u ON u.id_ubigeo = o.id_ubigeo
WHERE 1 = 1
"""

SQL_USUARIOS_ADMIN = """
SELECT us.id_usuario, us.nombre_completo, us.username, us.telefono, us.rol, us.activo,
       us.intentos_fallidos, us.bloqueado_hasta,
       COALESCE(us.bloqueado_hasta > CURRENT_TIMESTAMP, FALSE) AS bloqueado,
       us.fecha_creacion, us.id_olla, o.codigo_unico AS codigo_olla, o.nombre AS nombre_olla,
       o.activada AS olla_activada
FROM dbo.usuarios us
LEFT JOIN dbo.ollas o ON o.id_olla = us.id_olla
WHERE 1 = 1
"""


@router.get("/ollas", summary="Todas las ollas (activas e inactivas)")
def listar_ollas(
    activada: bool | None = Query(default=None),
    q: str | None = Query(default=None, max_length=100, description="Buscar por nombre o código"),
    conn: psycopg.Connection = Depends(get_db),
):
    sql, params = SQL_OLLAS_ADMIN, []
    if activada is not None:
        sql += " AND o.activada = ?"
        params.append(activada)
    if q:
        sql += " AND (o.nombre ILIKE ? OR o.codigo_unico ILIKE ?)"
        params.extend([f"%{q.strip()}%"] * 2)
    return consultar(conn, sql + " ORDER BY o.id_olla", *params)


@router.patch("/ollas/{id_olla}/estado", summary="Activar o desactivar una olla")
def cambiar_estado_olla(id_olla: int, datos: OllaEstadoAdmin, conn: psycopg.Connection = Depends(get_db)):
    if ejecutar(conn, "UPDATE dbo.ollas SET activada = ? WHERE id_olla = ?", datos.activada, id_olla) == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="La olla común indicada no existe.")
    olla = consultar_uno(conn, SQL_OLLAS_ADMIN + " AND o.id_olla = ?", id_olla)
    accion = "activada y visible en el portal" if datos.activada else "desactivada; ya no es visible y sus usuarias no podrán ingresar"
    return {"mensaje": f"La olla {olla['codigo_unico']} fue {accion}.", "olla": olla}


@router.get("/usuarios", summary="Todos los usuarios con su olla, rol y estado de bloqueo")
def listar_usuarios(
    rol: str | None = Query(default=None, description="ADMIN o DIRIGENTA"),
    activo: bool | None = Query(default=None),
    q: str | None = Query(default=None, max_length=100, description="Buscar por nombre o usuario"),
    conn: psycopg.Connection = Depends(get_db),
):
    sql, params = SQL_USUARIOS_ADMIN, []
    if rol:
        sql += " AND us.rol = ?"
        params.append(rol.strip().upper())
    if activo is not None:
        sql += " AND us.activo = ?"
        params.append(activo)
    if q:
        sql += " AND (us.nombre_completo ILIKE ? OR us.username ILIKE ?)"
        params.extend([f"%{q.strip()}%"] * 2)
    return consultar(conn, sql + " ORDER BY us.id_usuario", *params)


@router.patch("/usuarios/{id_usuario}", summary="Activar/desactivar, editar datos o desbloquear un usuario")
def actualizar_usuario(
    id_usuario: int,
    datos: UsuarioAdminUpdate,
    admin: dict = Depends(requiere_admin),
    conn: psycopg.Connection = Depends(get_db),
):
    if consultar_uno(conn, "SELECT id_usuario FROM dbo.usuarios WHERE id_usuario = ?", id_usuario) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="El usuario indicado no existe.")

    campos = datos.model_fields_set
    if "activo" in campos and datos.activo is False and id_usuario == admin["id_usuario"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No puede desactivar su propia cuenta de administrador.")

    asignaciones: list[str] = []
    params: list = []
    acciones: list[str] = []
    if "activo" in campos and datos.activo is not None:
        asignaciones.append("activo = ?")
        params.append(datos.activo)
        acciones.append("activado" if datos.activo else "desactivado")
    if "nombre_completo" in campos and datos.nombre_completo:
        asignaciones.append("nombre_completo = ?")
        params.append(datos.nombre_completo)
        acciones.append("nombre actualizado")
    if "telefono" in campos:
        asignaciones.append("telefono = ?")
        params.append(datos.telefono)
        acciones.append("teléfono actualizado")
    if datos.desbloquear:
        asignaciones.extend(["intentos_fallidos = 0", "bloqueado_hasta = NULL"])
        acciones.append("desbloqueado")

    if not asignaciones:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No se envió ningún cambio para aplicar.")

    ejecutar(conn, f"UPDATE dbo.usuarios SET {', '.join(asignaciones)} WHERE id_usuario = ?", *params, id_usuario)
    usuario = consultar_uno(conn, SQL_USUARIOS_ADMIN + " AND us.id_usuario = ?", id_usuario)
    return {"mensaje": f"Usuario {usuario['username']}: {', '.join(acciones)}.", "usuario": usuario}
