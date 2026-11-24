"""HU-08: Panel de la dirigenta - estado diario y necesidades de su olla."""

import psycopg
from fastapi import APIRouter, Depends, HTTPException, status

from app.core.database import consultar_uno, ejecutar, get_db
from app.apis.auth import SQL_OLLA_RESUMEN
from app.schemas import EstadoDiario
from app.core.security import get_id_olla_contexto

router = APIRouter(prefix="/api/olla", tags=["Panel dirigenta"])


@router.get("/mi-olla", summary="Datos de la olla vinculada al usuario")
def mi_olla(id_olla: int = Depends(get_id_olla_contexto), conn: psycopg.Connection = Depends(get_db)):
    olla = consultar_uno(conn, SQL_OLLA_RESUMEN, id_olla)
    if olla is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No se encontró la olla común.")
    return olla


@router.put("/estado-diario", summary="Actualizar atención del día, frescos, beneficiarios y necesidades")
def actualizar_estado(
    datos: EstadoDiario,
    id_olla: int = Depends(get_id_olla_contexto),
    conn: psycopg.Connection = Depends(get_db),
):
    cambios = datos.model_dump(include=datos.model_fields_set)
    if "necesidades_urgentes" in cambios:
        cambios["necesidades_urgentes"] = (cambios["necesidades_urgentes"] or "").strip() or None
    for campo in ("atiende_hoy", "recibe_frescos", "beneficiarios"):
        if campo in cambios and cambios[campo] is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"El campo «{campo.replace('_', ' ')}» no puede quedar vacío.",
            )

    asignaciones = ", ".join(f"{campo} = ?" for campo in cambios)
    ejecutar(conn, f"UPDATE dbo.ollas SET {asignaciones} WHERE id_olla = ?", *cambios.values(), id_olla)
    return {
        "mensaje": "El estado diario de su olla fue actualizado y ya se muestra en el portal público.",
        "olla": consultar_uno(conn, SQL_OLLA_RESUMEN, id_olla),
    }
