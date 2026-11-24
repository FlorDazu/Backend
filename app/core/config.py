"""Configuración centralizada de la API AYNI (pydantic-settings).

Todas las variables de entorno se leen **solo aquí** (desde `backend/.env`).
El resto del código importa el objeto `settings`:

    from app.core.config import settings
    settings.database_url

Cada campo tiene un valor por defecto seguro, de modo que la app y los tests
unitarios cargan aunque no exista `.env` (la BD responderá 503 por petición).
"""

import logging
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

logger = logging.getLogger("ayni.config")

# backend/.env con ruta absoluta (parents[0]=core, parents[1]=app, parents[2]=backend),
# así funciona igual al lanzar uvicorn/pytest desde cualquier carpeta.
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

JWT_SECRET_EJEMPLO = "ayni_super_secret_jwt_key_2026_cambiar"
ORIGEN_FRONTEND = "http://localhost:4200"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    # --- Base de datos: Supabase (Session pooler :5432) ----------------------
    # Cadena completa; tiene prioridad sobre DB_*. La contraseña debe ir URL-encodeada.
    database_url: str = ""
    # Respaldo por partes (solo si DATABASE_URL está vacía); la clave va tal cual.
    db_server: str = "localhost"
    db_port: int = 5432
    db_name: str = "postgres"
    db_user: str = "postgres"
    db_password: str = ""
    db_sslmode: str = "prefer"

    # --- Pool de conexiones (psycopg_pool) ------------------------------------
    db_pool_min: int = 1
    db_pool_max: int = 10
    db_timeout: float = 5.0

    # --- Supabase (opcional: REST/Storage/Auth; la BD no las necesita) --------
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""  # nunca debe llegar al frontend

    # --- Seguridad (JWT) -------------------------------------------------------
    jwt_secret: str = JWT_SECRET_EJEMPLO
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 480

    # --- CORS: "http://localhost:4200,http://127.0.0.1:4200" -------------------
    cors_origins: Annotated[list[str], NoDecode] = []

    # --- Recetario externo -------------------------------------------------------
    api_ninjas_key: str = ""
    # Opcional: clave de Google Cloud Translation API (traducción oficial y estable).
    # Sin ella se usan traductores gratuitos (Google sin clave → MyMemory).
    google_translate_api_key: str = ""

    @field_validator("cors_origins", mode="before")
    @classmethod
    def separar_origenes(cls, valor: Any) -> Any:
        """Convierte la lista separada por comas del .env en list[str]."""
        if isinstance(valor, str):
            return [origen.strip() for origen in valor.split(",") if origen.strip()]
        return valor

    @field_validator("database_url", "api_ninjas_key", "google_translate_api_key", "jwt_secret", mode="before")
    @classmethod
    def recortar(cls, valor: Any) -> Any:
        return valor.strip() if isinstance(valor, str) else valor

    @model_validator(mode="after")
    def incluir_frontend(self) -> "Settings":
        # http://localhost:4200 (Angular) siempre está permitido.
        if ORIGEN_FRONTEND not in self.cors_origins:
            self.cors_origins.append(ORIGEN_FRONTEND)
        return self

    @property
    def dsn(self) -> str:
        """Cadena de conexión final: DATABASE_URL o, si falta, armada desde DB_*."""
        if self.database_url:
            return self.database_url
        usuario = quote(self.db_user, safe="")
        clave = quote(self.db_password, safe="")
        return (
            f"postgresql://{usuario}:{clave}@{self.db_server.split(',')[0].strip()}:{self.db_port}"
            f"/{self.db_name}?sslmode={self.db_sslmode}"
        )

    def advertencias(self) -> list[str]:
        """Problemas de configuración que conviene revisar (sin exponer secretos)."""
        avisos = []
        if not self.database_url:
            avisos.append("DATABASE_URL no está definida; se usará DB_SERVER/DB_* como respaldo.")
        elif "pooler.supabase.com" in self.database_url and "sslmode=" not in self.database_url:
            avisos.append("DATABASE_URL de Supabase sin 'sslmode=require'.")
        if self.jwt_secret == JWT_SECRET_EJEMPLO:
            avisos.append("JWT_SECRET usa el valor de ejemplo; cámbielo en backend/.env.")
        if not self.api_ninjas_key:
            avisos.append("API_NINJAS_KEY no está definida; el recetario externo usará recetas locales.")
        return avisos


settings = Settings()
