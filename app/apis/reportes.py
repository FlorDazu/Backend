"""HU-14: Kardex correlativo, reporte consolidado y exportación CSV."""

import csv
import io
from datetime import date, datetime, timedelta
from typing import Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from app.core.database import consultar, consultar_uno, get_db
from app.apis.inventario import SQL_STOCK
from app.core.security import get_id_olla_contexto

router = APIRouter(prefix="/api", tags=["Kardex y reportes"])

SQL_MOVIMIENTOS = """
SELECT m.id_movimiento, m.codigo_correlativo, m.tipo_movimiento, m.fecha_movimiento,
       l.id_insumo, i.nombre AS insumo, i.unidad_base,
       m.id_lote, l.codigo_lote, m.cantidad, m.saldo_anterior, m.saldo_posterior, m.motivo,
       m.id_usuario, u.nombre_completo AS usuario
FROM dbo.movimientos_inventario m
JOIN dbo.lotes_inventario l ON l.id_lote = m.id_lote
JOIN dbo.insumos i ON i.id_insumo = l.id_insumo
LEFT JOIN dbo.usuarios u ON u.id_usuario = m.id_usuario
WHERE l.id_olla = ?
"""


def _validar_rango(fecha_desde: date | None, fecha_hasta: date | None) -> None:
    if fecha_desde and fecha_hasta and fecha_desde > fecha_hasta:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="La fecha inicial no puede ser posterior a la fecha final.",
        )


def _filtrar_movimientos(
    id_olla: int,
    tipo: str | None,
    id_insumo: int | None,
    fecha_desde: date | None,
    fecha_hasta: date | None,
) -> tuple[str, list]:
    _validar_rango(fecha_desde, fecha_hasta)
    sql, params = SQL_MOVIMIENTOS, [id_olla]
    if tipo:
        sql += " AND m.tipo_movimiento = ?"
        params.append(tipo)
    if id_insumo:
        sql += " AND l.id_insumo = ?"
        params.append(id_insumo)
    if fecha_desde:
        sql += " AND m.fecha_movimiento >= ?"
        params.append(datetime.combine(fecha_desde, datetime.min.time()))
    if fecha_hasta:
        sql += " AND m.fecha_movimiento < ?"
        params.append(datetime.combine(fecha_hasta + timedelta(days=1), datetime.min.time()))
    return sql, params


@router.get("/movimientos", summary="Kardex correlativo de movimientos (#E-, #S-, #D-)")
def movimientos(
    tipo: Literal["ENTRADA", "SALIDA", "DESCARTE"] | None = Query(default=None),
    id_insumo: int | None = Query(default=None, gt=0),
    fecha_desde: date | None = Query(default=None, description="AAAA-MM-DD"),
    fecha_hasta: date | None = Query(default=None, description="AAAA-MM-DD"),
    limite: int = Query(default=500, ge=1, le=5000),
    id_olla: int = Depends(get_id_olla_contexto),
    conn: psycopg.Connection = Depends(get_db),
):
    sql, params = _filtrar_movimientos(id_olla, tipo, id_insumo, fecha_desde, fecha_hasta)
    return consultar(conn, sql + " ORDER BY m.fecha_movimiento DESC, m.id_movimiento DESC LIMIT ?", *params, limite)


@router.get("/reportes/resumen", summary="Reporte consolidado de la olla (JSON)")
def resumen(
    fecha_desde: date | None = Query(default=None, description="AAAA-MM-DD"),
    fecha_hasta: date | None = Query(default=None, description="AAAA-MM-DD"),
    id_olla: int = Depends(get_id_olla_contexto),
    conn: psycopg.Connection = Depends(get_db),
):
    _validar_rango(fecha_desde, fecha_hasta)
    desde = datetime.combine(fecha_desde, datetime.min.time()) if fecha_desde else datetime(1900, 1, 1)
    hasta = datetime.combine((fecha_hasta or date(9998, 12, 30)) + timedelta(days=1), datetime.min.time())

    olla = consultar_uno(
        conn,
        """SELECT o.id_olla, o.codigo_unico, o.nombre, o.beneficiarios, o.atiende_hoy, o.activada,
                  u.departamento, u.provincia, u.distrito
           FROM dbo.ollas o JOIN dbo.ubigeos u ON u.id_ubigeo = o.id_ubigeo WHERE o.id_olla = ?""",
        id_olla,
    )
    donaciones = consultar_uno(
        conn,
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN tipo_donacion = 'ESPECIE' THEN 1 ELSE 0 END) AS en_especie,
                  SUM(CASE WHEN tipo_donacion = 'MONETARIA' THEN 1 ELSE 0 END) AS monetarias,
                  ISNULL(SUM(monto_soles), 0) AS monto_total_soles,
                  COUNT(DISTINCT id_donante) AS donantes_distintos
           FROM dbo.donaciones WHERE id_olla = ? AND fecha_donacion >= ? AND fecha_donacion < ?""",
        id_olla, desde, hasta,
    )
    movs = consultar(
        conn,
        """SELECT m.tipo_movimiento, COUNT(*) AS cantidad_movimientos, SUM(m.cantidad) AS cantidad_total
           FROM dbo.movimientos_inventario m
           JOIN dbo.lotes_inventario l ON l.id_lote = m.id_lote
           WHERE l.id_olla = ? AND m.fecha_movimiento >= ? AND m.fecha_movimiento < ?
           GROUP BY m.tipo_movimiento""",
        id_olla, desde, hasta,
    )
    despachos = consultar_uno(
        conn,
        """SELECT COUNT(*) AS total_despachos, ISNULL(SUM(raciones_servidas), 0) AS raciones_servidas
           FROM dbo.despachos_receta WHERE id_olla = ? AND fecha_despacho >= ? AND fecha_despacho < ?""",
        id_olla, desde, hasta,
    )
    semaforo = consultar(
        conn,
        """SELECT semaforo, COUNT(*) AS lotes FROM (
               SELECT CASE
                   WHEN fecha_vencimiento IS NULL THEN 'VIGENTE'
                   WHEN fecha_vencimiento < CURRENT_DATE THEN 'VENCIDO'
                   WHEN (fecha_vencimiento - CURRENT_DATE) <= 7 THEN 'POR_VENCER'
                   ELSE 'VIGENTE' END AS semaforo
               FROM dbo.lotes_inventario
               WHERE id_olla = ? AND estado = 'ACTIVO' AND cantidad_disponible > 0
           ) t GROUP BY semaforo""",
        id_olla,
    )
    stock = consultar(conn, SQL_STOCK, id_olla)
    stock_bajo = [
        {"id_insumo": s["id_insumo"], "insumo": s["nombre"], "stock_actual": s["stock_actual"],
         "stock_minimo": s["stock_minimo"], "unidad_base": s["unidad_base"]}
        for s in stock if s["stock_actual"] < s["stock_minimo"]
    ]

    movimientos_por_tipo = {t: {"cantidad_movimientos": 0, "cantidad_total": 0} for t in ("ENTRADA", "SALIDA", "DESCARTE")}
    for m in movs:
        movimientos_por_tipo[m["tipo_movimiento"]] = {
            "cantidad_movimientos": m["cantidad_movimientos"], "cantidad_total": m["cantidad_total"]
        }
    lotes_semaforo = {"VIGENTE": 0, "POR_VENCER": 0, "VENCIDO": 0}
    for s in semaforo:
        lotes_semaforo[s["semaforo"]] = s["lotes"]

    return {
        "generado_en": datetime.now(),
        "periodo": {"desde": fecha_desde, "hasta": fecha_hasta},
        "olla": olla,
        "donaciones": donaciones,
        "movimientos_por_tipo": movimientos_por_tipo,
        "despachos": despachos,
        "lotes_activos_por_semaforo": lotes_semaforo,
        "insumos_stock_bajo": stock_bajo,
        "stock": stock,
    }


@router.get("/reportes/exportar-csv", summary="Descargar reporte en CSV (kardex, stock o donaciones)")
def exportar_csv(
    tipo: Literal["movimientos", "stock", "donaciones"] = Query(default="movimientos"),
    fecha_desde: date | None = Query(default=None, description="AAAA-MM-DD"),
    fecha_hasta: date | None = Query(default=None, description="AAAA-MM-DD"),
    id_olla: int = Depends(get_id_olla_contexto),
    conn: psycopg.Connection = Depends(get_db),
):
    if tipo == "movimientos":
        sql, params = _filtrar_movimientos(id_olla, None, None, fecha_desde, fecha_hasta)
        filas = consultar(conn, sql + " ORDER BY m.id_movimiento", *params)
        columnas = ["codigo_correlativo", "fecha_movimiento", "tipo_movimiento", "insumo", "unidad_base",
                    "codigo_lote", "cantidad", "saldo_anterior", "saldo_posterior", "motivo", "usuario"]
    elif tipo == "stock":
        filas = consultar(conn, SQL_STOCK, id_olla)
        for f in filas:
            f["alerta_stock_bajo"] = "SI" if f["stock_actual"] < f["stock_minimo"] else "NO"
        columnas = ["nombre", "categoria", "unidad_base", "stock_actual", "stock_minimo", "alerta_stock_bajo",
                    "stock_vencido", "lotes_activos", "proximo_vencimiento"]
    else:
        _validar_rango(fecha_desde, fecha_hasta)
        sql = """SELECT dn.id_donacion, dn.fecha_donacion, dn.tipo_donacion, d.nombre_razon_social AS donante,
                        dn.monto_soles, dn.comprobante, dn.descripcion
                 FROM dbo.donaciones dn LEFT JOIN dbo.donantes d ON d.id_donante = dn.id_donante
                 WHERE dn.id_olla = ?"""
        params = [id_olla]
        if fecha_desde:
            sql += " AND dn.fecha_donacion >= ?"
            params.append(datetime.combine(fecha_desde, datetime.min.time()))
        if fecha_hasta:
            sql += " AND dn.fecha_donacion < ?"
            params.append(datetime.combine(fecha_hasta + timedelta(days=1), datetime.min.time()))
        filas = consultar(conn, sql + " ORDER BY dn.fecha_donacion", *params)
        columnas = ["id_donacion", "fecha_donacion", "tipo_donacion", "donante", "monto_soles", "comprobante", "descripcion"]

    buffer = io.StringIO()
    buffer.write("﻿")  # BOM para que Excel reconozca tildes y ñ
    escritor = csv.DictWriter(buffer, fieldnames=columnas, extrasaction="ignore", delimiter=";")
    escritor.writeheader()
    for fila in filas:
        escritor.writerow({c: ("" if fila.get(c) is None else fila.get(c)) for c in columnas})
    buffer.seek(0)

    nombre = f"ayni_{tipo}_olla{id_olla}_{date.today():%Y%m%d}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
    )
