"""HU-01 a HU-04: Portal público (KPIs, búsqueda geográfica, ficha y registro directo)."""

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.database import consultar, consultar_uno, ejecutar_sp, get_db
from app.schemas import OllaRegistro

router = APIRouter(prefix="/api/public", tags=["Portal público"])

SQL_OLLA_PUBLICA = """
SELECT o.id_olla, o.codigo_unico, o.nombre, o.direccion, o.referencia, o.telefono,
       o.beneficiarios, o.atiende_hoy, o.recibe_frescos, o.necesidades_urgentes,
       o.verificada, o.fecha_registro,
       u.id_ubigeo, u.departamento, u.provincia, u.distrito
FROM dbo.ollas o
JOIN dbo.ubigeos u ON u.id_ubigeo = o.id_ubigeo
WHERE o.activada = 1
"""


@router.get("/kpis", summary="Indicadores de impacto en tiempo real")
def kpis(conn: psycopg.Connection = Depends(get_db)):
    return consultar_uno(
        conn,
        """SELECT
            (SELECT COUNT(*) FROM dbo.ollas WHERE activada = 1) AS ollas_activas,
            (SELECT COUNT(*) FROM dbo.ollas WHERE activada = 1 AND atiende_hoy = 1) AS ollas_atienden_hoy,
            (SELECT ISNULL(SUM(beneficiarios), 0) FROM dbo.ollas WHERE activada = 1 AND atiende_hoy = 1) AS beneficiarios_hoy,
            (SELECT ISNULL(SUM(beneficiarios), 0) FROM dbo.ollas WHERE activada = 1) AS beneficiarios_totales,
            (SELECT COUNT(DISTINCT id_ubigeo) FROM dbo.ollas WHERE activada = 1) AS distritos_cubiertos,
            (SELECT COUNT(*) FROM dbo.donaciones) AS total_donaciones,
            (SELECT ISNULL(SUM(monto_soles), 0) FROM dbo.donaciones WHERE tipo_donacion = 'MONETARIA') AS monto_donado_soles,
            (SELECT COUNT(*) FROM dbo.movimientos_inventario) AS total_movimientos""",
    )


@router.get("/ubigeos", summary="Catálogo geográfico (departamento, provincia, distrito)")
def ubigeos(
    departamento: str | None = Query(default=None, max_length=80),
    provincia: str | None = Query(default=None, max_length=80),
    conn: psycopg.Connection = Depends(get_db),
):
    sql = "SELECT id_ubigeo, departamento, provincia, distrito FROM dbo.ubigeos WHERE 1 = 1"
    params: list = []
    if departamento:
        sql += " AND departamento = ?"
        params.append(departamento.strip())
    if provincia:
        sql += " AND provincia = ?"
        params.append(provincia.strip())
    sql += " ORDER BY departamento, provincia, distrito"
    return consultar(conn, sql, *params)


@router.get("/ollas", summary="Ollas comunes activas con filtros geográficos")
def listar_ollas(
    departamento: str | None = Query(default=None, max_length=80),
    provincia: str | None = Query(default=None, max_length=80),
    distrito: str | None = Query(default=None, max_length=80),
    id_ubigeo: int | None = Query(default=None, gt=0),
    atiende_hoy: bool | None = Query(default=None),
    recibe_frescos: bool | None = Query(default=None),
    q: str | None = Query(default=None, max_length=100, description="Buscar por nombre o código"),
    conn: psycopg.Connection = Depends(get_db),
):
    sql = SQL_OLLA_PUBLICA
    params: list = []
    filtros = {
        "u.departamento": departamento,
        "u.provincia": provincia,
        "u.distrito": distrito,
        "u.id_ubigeo": id_ubigeo,
        "o.atiende_hoy": atiende_hoy,
        "o.recibe_frescos": recibe_frescos,
    }
    for columna, valor in filtros.items():
        if valor is None or valor == "":
            continue
        sql += f" AND {columna} = ?"
        params.append(valor.strip() if isinstance(valor, str) else valor)
    if q:
        sql += " AND (o.nombre ILIKE ? OR o.codigo_unico ILIKE ?)"
        patron = f"%{q.strip()}%"
        params.extend([patron, patron])
    sql += " ORDER BY o.atiende_hoy DESC, o.nombre"
    return consultar(conn, sql, *params)


@router.get("/ollas/{id_olla}", summary="Ficha pública detallada de una olla")
def detalle_olla(id_olla: int, conn: psycopg.Connection = Depends(get_db)):
    olla = consultar_uno(conn, SQL_OLLA_PUBLICA + " AND o.id_olla = ?", id_olla)
    if olla is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="La olla común no existe o no se encuentra activa.",
        )
    return olla


@router.post("/ollas", status_code=status.HTTP_201_CREATED, summary="Registro directo de nueva olla común")
def registrar_olla(datos: OllaRegistro, conn: psycopg.Connection = Depends(get_db)):
    creada = ejecutar_sp(
        conn,
        """EXEC dbo.sp_RegistrarOlla @nombre = ?, @id_ubigeo = ?, @direccion = ?,
               @telefono = ?, @referencia = ?, @beneficiarios = ?""",
        datos.nombre,
        datos.id_ubigeo,
        datos.direccion,
        datos.telefono,
        datos.referencia,
        datos.beneficiarios,
    )
    olla = consultar_uno(conn, SQL_OLLA_PUBLICA + " AND o.id_olla = ?", creada["id_olla"])
    return {
        "mensaje": f"La olla común fue registrada con el código {creada['codigo_unico']} y ya es visible en el portal.",
        "olla": olla,
    }
