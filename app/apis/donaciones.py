"""HU-09 / HU-10: Donantes (registro manual) y donaciones monetarias o en especie."""

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.database import consultar, consultar_uno, ejecutar_en_cursor, ejecutar_sp, get_db, transaccion
from app.schemas import DonacionCrear, DonanteCrear
from app.core.security import get_id_olla_contexto, get_usuario_actual

router = APIRouter(prefix="/api", tags=["Donantes y donaciones"])

SQL_DONANTE = """
SELECT d.id_donante, d.tipo_donante, d.nombre_razon_social, d.documento, d.telefono,
       d.direccion, d.es_frecuente, d.fecha_registro,
       (SELECT COUNT(*) FROM dbo.donaciones x WHERE x.id_donante = d.id_donante) AS total_donaciones
FROM dbo.donantes d
WHERE d.id_olla = ?
"""


# ---------------------------------------------------------------------------
# Donantes
# ---------------------------------------------------------------------------

@router.get("/donantes", summary="Directorio de donantes de la olla")
def listar_donantes(
    tipo_donante: str | None = Query(default=None, description="PERSONA_NATURAL, EMPRESA, MERCADO o INSTITUCION"),
    es_frecuente: bool | None = None,
    q: str | None = Query(default=None, max_length=100, description="Buscar por nombre o documento"),
    id_olla: int = Depends(get_id_olla_contexto),
    conn: psycopg.Connection = Depends(get_db),
):
    sql, params = SQL_DONANTE, [id_olla]
    if tipo_donante:
        sql += " AND d.tipo_donante = ?"
        params.append(tipo_donante.strip().upper())
    if es_frecuente is not None:
        sql += " AND d.es_frecuente = ?"
        params.append(es_frecuente)
    if q:
        sql += " AND (d.nombre_razon_social ILIKE ? OR d.documento ILIKE ?)"
        params.extend([f"%{q.strip()}%"] * 2)
    sql += " ORDER BY d.es_frecuente DESC, d.nombre_razon_social"
    return consultar(conn, sql, *params)


@router.post("/donantes", status_code=status.HTTP_201_CREATED, summary="Registrar donante manualmente")
def crear_donante(
    datos: DonanteCrear,
    id_olla: int = Depends(get_id_olla_contexto),
    conn: psycopg.Connection = Depends(get_db),
):
    if datos.documento and consultar_uno(
        conn, "SELECT 1 AS existe FROM dbo.donantes WHERE id_olla = ? AND documento = ?", id_olla, datos.documento
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Ya existe un donante registrado con el documento {datos.documento} en su olla.",
        )
    id_donante = ejecutar_sp(
        conn,
        """INSERT INTO dbo.donantes (id_olla, tipo_donante, nombre_razon_social, documento, telefono, direccion, es_frecuente)
           OUTPUT inserted.id_donante
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        id_olla,
        datos.tipo_donante,
        datos.nombre_razon_social,
        datos.documento,
        datos.telefono,
        datos.direccion,
        datos.es_frecuente,
    )["id_donante"]
    donante = consultar_uno(conn, SQL_DONANTE + " AND d.id_donante = ?", id_olla, id_donante)
    return {"mensaje": f"El donante «{datos.nombre_razon_social}» fue registrado correctamente.", "donante": donante}


# ---------------------------------------------------------------------------
# Donaciones
# ---------------------------------------------------------------------------

@router.get("/donaciones", summary="Donaciones recibidas por la olla")
def listar_donaciones(
    tipo_donacion: str | None = Query(default=None, description="ESPECIE o MONETARIA"),
    id_donante: int | None = Query(default=None, gt=0),
    id_olla: int = Depends(get_id_olla_contexto),
    conn: psycopg.Connection = Depends(get_db),
):
    sql = """
        SELECT dn.id_donacion, dn.tipo_donacion, dn.monto_soles, dn.comprobante, dn.descripcion,
               dn.fecha_donacion, dn.id_donante, d.nombre_razon_social AS donante, d.tipo_donante
        FROM dbo.donaciones dn
        LEFT JOIN dbo.donantes d ON d.id_donante = dn.id_donante
        WHERE dn.id_olla = ?"""
    params: list = [id_olla]
    if tipo_donacion:
        sql += " AND dn.tipo_donacion = ?"
        params.append(tipo_donacion.strip().upper())
    if id_donante:
        sql += " AND dn.id_donante = ?"
        params.append(id_donante)
    sql += " ORDER BY dn.fecha_donacion DESC, dn.id_donacion DESC"
    donaciones = consultar(conn, sql, *params)

    items = consultar(
        conn,
        """SELECT l.id_donacion, l.id_lote, l.codigo_lote, l.id_insumo, i.nombre AS insumo, i.unidad_base,
                  l.cantidad_inicial, l.cantidad_disponible, l.fecha_vencimiento, l.estado
           FROM dbo.lotes_inventario l
           JOIN dbo.insumos i ON i.id_insumo = l.id_insumo
           WHERE l.id_olla = ? AND l.id_donacion IS NOT NULL
           ORDER BY l.id_lote""",
        id_olla,
    )
    por_donacion: dict[int, list] = {}
    for item in items:
        por_donacion.setdefault(item.pop("id_donacion"), []).append(item)
    for donacion in donaciones:
        donacion["items"] = por_donacion.get(donacion["id_donacion"], [])
    return donaciones


@router.post("/donaciones", status_code=status.HTTP_201_CREATED, summary="Registrar donación monetaria o en especie")
def crear_donacion(
    datos: DonacionCrear,
    usuario: dict = Depends(get_usuario_actual),
    id_olla: int = Depends(get_id_olla_contexto),
    conn: psycopg.Connection = Depends(get_db),
):
    if datos.id_donante and not consultar_uno(
        conn, "SELECT 1 AS existe FROM dbo.donantes WHERE id_donante = ? AND id_olla = ?", datos.id_donante, id_olla
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="El donante indicado no pertenece a su olla.")

    if datos.items:
        ids = sorted({item.id_insumo for item in datos.items})
        marcadores = ", ".join("?" for _ in ids)
        insumos = {
            fila["id_insumo"]: fila
            for fila in consultar(
                conn,
                f"SELECT id_insumo, nombre, requiere_vencimiento FROM dbo.insumos WHERE id_insumo IN ({marcadores})",
                *ids,
            )
        }
        for posicion, item in enumerate(datos.items, start=1):
            insumo = insumos.get(item.id_insumo)
            if insumo is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"El insumo del ítem {posicion} no existe en el catálogo.",
                )
            if insumo["requiere_vencimiento"] and item.fecha_vencimiento is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"El insumo «{insumo['nombre']}» requiere fecha de vencimiento.",
                )

    lotes_creados = []
    with transaccion(conn) as cursor:
        id_donacion = ejecutar_en_cursor(
            cursor,
            """INSERT INTO dbo.donaciones (id_olla, id_donante, tipo_donacion, monto_soles, comprobante, descripcion)
               OUTPUT inserted.id_donacion
               VALUES (?, ?, ?, ?, ?, ?)""",
            id_olla,
            datos.id_donante,
            datos.tipo_donacion,
            datos.monto_soles,
            datos.comprobante,
            datos.descripcion,
        )[0]["id_donacion"]

        if datos.tipo_donacion == "ESPECIE":
            for item in datos.items:
                filas = ejecutar_en_cursor(
                    cursor,
                    """EXEC dbo.sp_RegistrarEntradaInventario
                        @id_olla = ?, @id_insumo = ?, @id_usuario = ?, @id_donacion = ?,
                        @cantidad = ?, @fecha_vencimiento = ?, @motivo = ?""",
                    id_olla,
                    item.id_insumo,
                    usuario["id_usuario"],
                    id_donacion,
                    item.cantidad,
                    item.fecha_vencimiento,
                    f"Ingreso por donación #{id_donacion}: {datos.descripcion}"[:250],
                )
                lote = filas[0] if filas else None
                if lote:
                    lote["id_insumo"] = item.id_insumo
                    lotes_creados.append(lote)

    if datos.tipo_donacion == "MONETARIA":
        mensaje = f"Donación monetaria de S/ {datos.monto_soles:.2f} registrada correctamente."
    elif lotes_creados:
        mensaje = f"Donación en especie registrada. Se ingresaron {len(lotes_creados)} lote(s) al inventario."
    else:
        mensaje = "Donación en especie registrada correctamente."
    return {"mensaje": mensaje, "id_donacion": id_donacion, "lotes": lotes_creados}
