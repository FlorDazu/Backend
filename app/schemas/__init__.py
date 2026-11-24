"""Capa de esquemas Pydantic: validaciones y modelos de entrada de la API.

`app.schemas` sigue siendo el punto de importación público de siempre:
    from app.schemas import OllaRegistro

El paquete reexporta todo desde `app/schemas/schemas.py` para que ningún
import existente tenga que cambiar.
"""

from .schemas import *  # noqa: F401,F403 - reexporta todos los esquemas públicos
