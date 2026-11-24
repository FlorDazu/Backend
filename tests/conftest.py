"""Configuración común de las pruebas de la API AYNI."""

import sys
from pathlib import Path

# Permite importar `app.*` aunque pytest se lance desde cualquier carpeta.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from app.main import app


class ConexionFalsa:
    """Doble de conexión para no necesitar PostgreSQL en los tests.

    Solo se usan los métodos que las dependencias `Depends(get_db)` llegan a
    tocar en las rutas que se prueban; cualquier intento real de consulta
    fallará de forma evidente en lugar de devolver datos inventados.
    """

    def close(self) -> None:  # noqa: D401 - llamado por el `finally` de get_db
        pass

    def cursor(self, *args, **kwargs):  # pragma: no cover - defensa
        raise AssertionError("Una prueba intentó ejecutar SQL sin base de datos.")


@pytest.fixture()
def cliente(monkeypatch) -> TestClient:
    """Cliente de prueba con la conexión a la base de datos sustituida."""
    monkeypatch.setattr("app.core.database.obtener_conexion", lambda: ConexionFalsa())
    return TestClient(app)
