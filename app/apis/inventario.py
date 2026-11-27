"""HU-11: Inventario PEPS - catálogo, stock consolidado, lotes con semáforo, entradas, salidas y descartes."""

from typing import Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.database import consultar, consultar_uno, ejecutar_sp, get_db
from app.schemas import DescarteLote, EntradaInventario, SalidaInventario
from app.core.security import get_id_olla_contexto, get_usuario_actual

router = APIRouter(prefix="/api", tags=["Inventario PEPS"])

DIAS_POR_VENCER = 7

SQL_STOCK = """
SELECT i.id_insumo, i.nombre, i.categoria, i.unidad_base, i.requiere_vencimiento, i.stock_minimo,
       ISNULL(SUM(CASE WHEN l.fecha_vencimiento IS NULL OR l.fecha_vencimiento >= CURRENT_DATE
                       THEN l.cantidad_disponible END), 0) AS stock_actual,
       ISNULL(SUM(CASE WHEN l.fecha_vencimiento < CURRENT_DATE
                       THEN l.cantidad_disponible END), 0) AS stock_vencido,
       COUNT(l.id_lote) AS lotes_activos,
       MIN(CASE WHEN l.fecha_vencimiento >= CURRENT_DATE THEN l.fecha_vencimiento END) AS proximo_vencimiento
FROM dbo.insumos i
LEFT JOIN dbo.lotes_inventario l
       ON l.id_insumo = i.id_insumo AND l.id_olla = ? AND l.estado = 'ACTIVO' AND l.cantidad_disponible > 0
GROUP BY i.id_insumo, i.nombre, i.categoria, i.unidad_base, i.requiere_vencimiento, i.stock_minimo
ORDER BY i.categoria, i.nombre
"""

SQL_LOTES = f"""
SELECT * FROM (
    SELECT l.id_lote, l.codigo_lote, l.id_insumo, i.nombre AS insumo, i.categoria, i.unidad_base,
           l.id_donacion, l.cantidad_inicial, l.cantidad_disponible, l.fecha_ingreso, l.fecha_vencimiento, l.estado,
           (l.fecha_vencimiento - CURRENT_DATE) AS dias_para_vencer,
           CASE
               WHEN l.fecha_vencimiento IS NULL THEN 'VIGENTE'
               WHEN l.fecha_vencimiento < CURRENT_DATE THEN 'VENCIDO'
               WHEN (l.fecha_vencimiento - CURRENT_DATE) <= {DIAS_POR_VENCER} THEN 'POR_VENCER'
               ELSE 'VIGENTE'
           END AS semaforo
    FROM dbo.lotes_inventario l
    JOIN dbo.insumos i ON i.id_insumo = l.id_insumo
    WHERE l.id_olla = ?
) t
WHERE 1 = 1
"""


@router.get("/insumos", summary="Catálogo de insumos")
def listar_insumos(
    categoria: str | None = Query(default=None, max_length=60),
    conn: psycopg.Connection = Depends(get_db),
    _: dict = Depends(get_usuario_actual),
):
    sql = "SELECT id_insumo, nombre, categoria, unidad_base, requiere_vencimiento, stock_minimo FROM dbo.insumos"
    params: list = []
    if categoria:
        sql += " WHERE categoria = ?"
        params.append(categoria.strip())
    return consultar(conn, sql + " ORDER BY categoria, nombre", *params)


@router.get("/inventario/stock", summary="Stock vigente consolidado por insumo con alerta de stock bajo")
def stock(
    solo_alertas: bool = Query(default=False, description="true = solo insumos con stock bajo"),
    id_olla: int = Depends(get_id_olla_contexto),
    conn: psycopg.Connection = Depends(get_db),
):
    filas = consultar(conn, SQL_STOCK, id_olla)
    for fila in filas:
        fila["alerta_stock_bajo"] = fila["stock_actual"] < fila["stock_minimo"]
        fila["faltante_para_minimo"] = max(fila["stock_minimo"] - fila["stock_actual"], 0)
    insumos = [f for f in filas if f["alerta_stock_bajo"]] if solo_alertas else filas
    return {
        "resumen": {
            "insumos_catalogo": len(filas),
            "insumos_con_stock": sum(1 for f in filas if f["stock_actual"] > 0),
            "insumos_stock_bajo": sum(1 for f in filas if f["alerta_stock_bajo"]),
            "insumos_con_vencidos": sum(1 for f in filas if f["stock_vencido"] > 0),
        },
        "insumos": insumos,
    }


@router.get("/inventario/lotes", summary="Lotes de la olla con días para vencer y semáforo")
def lotes(
    estado: Literal["ACTIVO", "AGOTADO", "DESCARTADO", "TODOS"] = Query(default="ACTIVO"),
    semaforo: Literal["VIGENTE", "POR_VENCER", "VENCIDO"] | None = Query(default=None),
    id_insumo: int | None = Query(default=None, gt=0),
    id_olla: int = Depends(get_id_olla_contexto),
    conn: psycopg.Connection = Depends(get_db),
):
    sql, params = SQL_LOTES, [id_olla]
    if estado != "TODOS":
        sql += " AND estado = ?"
        params.append(estado)
    if semaforo:
        sql += " AND semaforo = ?"
        params.append(semaforo)
    if id_insumo:
        sql += " AND id_insumo = ?"
        params.append(id_insumo)
    # Orden PEPS: primero en vencer, luego primero en entrar
    sql += " ORDER BY CASE WHEN fecha_vencimiento IS NULL THEN 1 ELSE 0 END, fecha_vencimiento, fecha_ingreso, id_lote"
    return consultar(conn, sql, *params)


@router.post("/inventario/entrada", status_code=status.HTTP_201_CREATED, summary="Registrar entrada manual (crea lote)")
def entrada(
    datos: EntradaInventario,
    usuario: dict = Depends(get_usuario_actual),
    id_olla: int = Depends(get_id_olla_contexto),
    conn: psycopg.Connection = Depends(get_db),
):
    insumo = consultar_uno(
        conn, "SELECT nombre, unidad_base, requiere_vencimiento FROM dbo.insumos WHERE id_insumo = ?", datos.id_insumo
    )
    if insumo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="El insumo indicado no existe en el catálogo.")
    if insumo["requiere_vencimiento"] and datos.fecha_vencimiento is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"El insumo «{insumo['nombre']}» requiere fecha de vencimiento.",
        )
    resultado = ejecutar_sp(
        conn,
        """EXEC dbo.sp_RegistrarEntradaInventario
            @id_olla = ?, @id_insumo = ?, @id_usuario = ?, @id_donacion = NULL,
            @cantidad = ?, @fecha_vencimiento = ?, @motivo = ?""",
        id_olla,
        datos.id_insumo,
        usuario["id_usuario"],
        datos.cantidad,
        datos.fecha_vencimiento,
        datos.motivo or "Ingreso manual de inventario",
    )
    return {
        "mensaje": f"Entrada registrada: {datos.cantidad} {insumo['unidad_base']} de {insumo['nombre']} "
        f"(lote #{resultado.get('codigo_lote')}, movimiento #{resultado.get('codigo_correlativo')}).",
        **resultado,
    }


@router.post("/inventario/salida", summary="Registrar salida por método PEPS")
def salida(
    datos: SalidaInventario,
    usuario: dict = Depends(get_usuario_actual),
    id_olla: int = Depends(get_id_olla_contexto),
    conn: psycopg.Connection = Depends(get_db),
):
    insumo = consultar_uno(conn, "SELECT nombre, unidad_base FROM dbo.insumos WHERE id_insumo = ?", datos.id_insumo)
    if insumo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="El insumo indicado no existe en el catálogo.")
    ultimo = consultar_uno(conn, "SELECT ISNULL(MAX(id_movimiento), 0) AS ultimo FROM dbo.movimientos_inventario")
    resultado = ejecutar_sp(
        conn,
        """EXEC dbo.sp_RegistrarSalidaPEPS
            @id_olla = ?, @id_insumo = ?, @id_usuario = ?, @cantidad = ?, @motivo = ?, @silencioso = 0""",
        id_olla,
        datos.id_insumo,
        usuario["id_usuario"],
        datos.cantidad,
        datos.motivo or "Salida de almacén",
    )
    movimientos = consultar(
        conn,
        """SELECT m.codigo_correlativo, l.codigo_lote, m.cantidad, m.saldo_posterior
           FROM dbo.movimientos_inventario m
           JOIN dbo.lotes_inventario l ON l.id_lote = m.id_lote
           WHERE m.id_movimiento > ? AND l.id_olla = ? AND l.id_insumo = ? AND m.id_usuario = ?
             AND m.tipo_movimiento = 'SALIDA'
           ORDER BY m.id_movimiento""",
        ultimo["ultimo"],
        id_olla,
        datos.id_insumo,
        usuario["id_usuario"],
    )
    return {
        "mensaje": f"Salida registrada por PEPS: {datos.cantidad} {insumo['unidad_base']} de {insumo['nombre']}. "
        f"Saldo restante: {resultado.get('saldo_restante')} {insumo['unidad_base']}.",
        **resultado,
        "movimientos": movimientos,
    }


@router.post("/inventario/descartar", summary="Descartar lote dañado o vencido (motivo obligatorio)")
def descartar(
    datos: DescarteLote,
    usuario: dict = Depends(get_usuario_actual),
    id_olla: int = Depends(get_id_olla_contexto),
    conn: psycopg.Connection = Depends(get_db),
):
    if not datos.motivo.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Debe indicar el motivo del descarte.")
    lote = consultar_uno(
        conn,
        """SELECT i.nombre, i.unidad_base FROM dbo.lotes_inventario l
           JOIN dbo.insumos i ON i.id_insumo = l.id_insumo WHERE l.id_lote = ? AND l.id_olla = ?""",
        datos.id_lote,
        id_olla,
    )
    if lote is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="El lote indicado no existe o no pertenece a su olla.")
    resultado = ejecutar_sp(
        conn,
        "EXEC dbo.sp_DescartarLote @id_olla = ?, @id_lote = ?, @id_usuario = ?, @motivo = ?",
        id_olla,
        datos.id_lote,
        usuario["id_usuario"],
        datos.motivo,
    )
    return {
        "mensaje": f"El lote #{resultado.get('codigo_lote')} de {lote['nombre']} fue descartado "
        f"({resultado.get('cantidad_descartada')} {lote['unidad_base']}). Movimiento #{resultado.get('codigo_correlativo')}.",
        **resultado,
    }
