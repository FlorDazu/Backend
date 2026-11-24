"""Pruebas básicas de la API AYNI (no requieren base de datos).

Cubren tres cosas: la documentación sigue sirviéndose, todos los routers
registran sus rutas tras la reorganización en `app/apis/`, y se validan los
datos de entrada (422) y el bloqueo de autenticación (401 / 403) con los
mensajes en español que define `app/main.py`.
"""

from app.core.security import crear_token

RUTAS_ESPERADAS = {
    # Portal público
    "/api/public/kpis",
    "/api/public/ollas",
    "/api/public/ollas/{id_olla}",
    "/api/public/ubigeos",
    # Autenticación
    "/api/auth/login",
    "/api/auth/me",
    "/api/auth/registro-olla",
    # Panel de la dirigenta
    "/api/olla/mi-olla",
    "/api/olla/estado-diario",
    "/api/inventario/stock",
    "/api/inventario/entrada",
    "/api/inventario/salida",
    "/api/inventario/lotes",
    "/api/donantes",
    "/api/donaciones",
    "/api/recetas",
    "/api/recetas/despachar",
    "/api/recetas/externas",
    "/api/movimientos",
    "/api/reportes/resumen",
    "/api/reportes/exportar-csv",
    # Administración
    "/api/admin/ollas",
    "/api/admin/usuarios",
}

USUARIO_DIRIGENTA = {
    "id_usuario": 1,
    "id_olla": 1,
    "nombre_completo": "Dirigenta de Prueba",
    "username": "dirigenta@ayni.org",
    "telefono": "987654321",
    "rol": "DIRIGENTA",
    "activo": True,
    "intentos_fallidos": 0,
    "bloqueado_hasta": None,
    "fecha_creacion": "2026-01-01T00:00:00",
    "codigo_unico": "AYNI-0001",
    "nombre_olla": "Olla de Prueba",
    "olla_activada": True,
}


# ---------------------------------------------------------------------------
# Documentación y salud
# ---------------------------------------------------------------------------

def test_documentacion_disponible(cliente):
    respuesta = cliente.get("/docs")
    assert respuesta.status_code == 200
    assert "AYNI" in respuesta.text


def test_openapi_registra_todas_las_apis(cliente):
    """Los ocho routers responden desde `app/apis` con sus prefijos intactos."""
    rutas = set(cliente.get("/openapi.json").json()["paths"])
    faltantes = RUTAS_ESPERADAS - rutas
    assert not faltantes, f"Faltan rutas en el esquema: {sorted(faltantes)}"


def test_raiz_informativa(cliente):
    respuesta = cliente.get("/")
    assert respuesta.status_code == 200
    assert "mensaje" in respuesta.json()


def test_salud_de_la_api(cliente):
    """La API responde aunque la base de datos esté apagada (200 + estado)."""
    respuesta = cliente.get("/api/health")
    cuerpo = respuesta.json()
    assert respuesta.status_code == 200
    assert cuerpo["api"] == "ok"
    assert cuerpo["base_datos"] in ("ok", "sin conexión")


# ---------------------------------------------------------------------------
# Validación de datos (422 en español)
# ---------------------------------------------------------------------------

def test_login_rechaza_cuerpo_vacio(cliente):
    respuesta = cliente.post("/api/auth/login", json={})
    assert respuesta.status_code == 422
    cuerpo = respuesta.json()
    assert cuerpo["detail"]
    assert "obligatorio" in cuerpo["detail"]
    assert cuerpo["errores"]


def test_registro_publico_rechaza_datos_incompletos(cliente):
    respuesta = cliente.post("/api/public/ollas", json={"nombre": "Olla sin ubicación"})
    assert respuesta.status_code == 422
    assert respuesta.json()["detail"]


# ---------------------------------------------------------------------------
# Bloqueo de autenticación (401 / 403)
# ---------------------------------------------------------------------------

def test_perfil_sin_token_es_401(cliente):
    respuesta = cliente.get("/api/auth/me")
    assert respuesta.status_code == 401
    assert respuesta.json()["detail"]


def test_rutas_protegidas_sin_token_son_401(cliente):
    for ruta in ("/api/admin/ollas", "/api/admin/usuarios", "/api/olla/mi-olla", "/api/inventario/stock"):
        respuesta = cliente.get(ruta)
        assert respuesta.status_code == 401, f"{ruta} respondió {respuesta.status_code}"


def test_rutas_de_admin_son_403_para_dirigenta(cliente, monkeypatch):
    monkeypatch.setattr("app.core.security.consultar_uno", lambda *args, **kwargs: USUARIO_DIRIGENTA)
    token = crear_token({"id_usuario": 1, "rol": "DIRIGENTA", "id_olla": 1})

    for ruta in ("/api/admin/ollas", "/api/admin/usuarios"):
        respuesta = cliente.get(ruta, headers={"Authorization": f"Bearer {token}"})
        assert respuesta.status_code == 403, f"{ruta} respondió {respuesta.status_code}"
        assert "administrador" in respuesta.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Errores en español
# ---------------------------------------------------------------------------

def test_ruta_desconocida_responde_404_en_espanol(cliente):
    respuesta = cliente.get("/api/ruta-que-no-existe")
    assert respuesta.status_code == 404
    assert respuesta.json()["detail"]
