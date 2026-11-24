"""Esquemas Pydantic de entrada con validaciones y mensajes en español (HU-05)."""

import re
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_RE_TELEFONO = re.compile(r"^\+?\d{6,15}$")


class EsquemaBase(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")


def _validar_telefono(valor: str | None, obligatorio: bool = True) -> str | None:
    if valor is None or valor == "":
        if obligatorio:
            raise ValueError("El teléfono es obligatorio.")
        return None
    limpio = re.sub(r"[\s\-()]", "", valor)
    if not _RE_TELEFONO.match(limpio):
        raise ValueError("El teléfono debe contener solo números (entre 6 y 15 dígitos), por ejemplo 987654321.")
    return limpio


def _texto_opcional(valor: str | None) -> str | None:
    if valor is None:
        return None
    valor = valor.strip()
    return valor or None


# ---------------------------------------------------------------------------
# Autenticación (HU-06 / HU-07)
# ---------------------------------------------------------------------------

class LoginRequest(EsquemaBase):
    username: str = Field(..., min_length=3, max_length=100, description="Usuario o correo electrónico")
    password: str = Field(..., min_length=1, max_length=128)

    @field_validator("username")
    @classmethod
    def normalizar_usuario(cls, v: str) -> str:
        return v.lower()


class RegistroOllaCuenta(EsquemaBase):
    """Cuenta de dirigenta + olla común en un solo envío."""

    nombre_completo: str = Field(..., min_length=3, max_length=150)
    username: str = Field(..., min_length=3, max_length=100, description="Usuario o correo electrónico")
    telefono: str
    password: str = Field(..., min_length=8, max_length=72)
    nombre_olla: str = Field(..., min_length=3, max_length=150)
    id_ubigeo: int = Field(..., gt=0)
    direccion: str = Field(..., min_length=5, max_length=250)
    referencia: str | None = Field(default=None, max_length=250)

    @field_validator("username")
    @classmethod
    def validar_usuario(cls, v: str) -> str:
        v = v.lower()
        if " " in v:
            raise ValueError("El usuario o correo no debe contener espacios.")
        if "@" in v and not re.match(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", v):
            raise ValueError("El correo electrónico no tiene un formato válido (ejemplo: nombre@correo.com).")
        return v

    @field_validator("telefono")
    @classmethod
    def validar_telefono(cls, v: str) -> str:
        return _validar_telefono(v)  # type: ignore[return-value]

    @field_validator("password")
    @classmethod
    def validar_password(cls, v: str) -> str:
        if not re.search(r"[A-Za-z]", v) or not re.search(r"\d", v):
            raise ValueError("La contraseña debe tener al menos 8 caracteres e incluir letras y números.")
        return v

    @field_validator("referencia")
    @classmethod
    def limpiar_referencia(cls, v: str | None) -> str | None:
        return _texto_opcional(v)


# ---------------------------------------------------------------------------
# Ollas (HU-04 / HU-08)
# ---------------------------------------------------------------------------

class OllaRegistro(EsquemaBase):
    nombre: str = Field(..., min_length=3, max_length=150)
    id_ubigeo: int = Field(..., gt=0)
    direccion: str = Field(..., min_length=5, max_length=250)
    telefono: str
    referencia: str | None = Field(default=None, max_length=250)
    beneficiarios: int = Field(default=0, ge=0, le=100000)

    @field_validator("telefono")
    @classmethod
    def validar_telefono(cls, v: str) -> str:
        return _validar_telefono(v)  # type: ignore[return-value]

    @field_validator("referencia")
    @classmethod
    def limpiar_referencia(cls, v: str | None) -> str | None:
        return _texto_opcional(v)


class EstadoDiario(EsquemaBase):
    atiende_hoy: bool | None = None
    recibe_frescos: bool | None = None
    beneficiarios: int | None = Field(default=None, ge=0, le=100000)
    necesidades_urgentes: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def al_menos_un_campo(self) -> "EstadoDiario":
        if not self.model_fields_set:
            raise ValueError("Debe enviar al menos un dato para actualizar el estado diario.")
        return self


# ---------------------------------------------------------------------------
# Donantes y donaciones (HU-09 / HU-10)
# ---------------------------------------------------------------------------

TipoDonante = Literal["PERSONA_NATURAL", "EMPRESA", "MERCADO", "INSTITUCION"]


class DonanteCrear(EsquemaBase):
    tipo_donante: TipoDonante
    nombre_razon_social: str = Field(..., min_length=2, max_length=150)
    documento: str | None = Field(default=None, max_length=20)
    telefono: str | None = None
    direccion: str | None = Field(default=None, max_length=250)
    es_frecuente: bool = False

    @field_validator("tipo_donante", mode="before")
    @classmethod
    def mayusculas(cls, v):
        return v.strip().upper() if isinstance(v, str) else v

    @field_validator("documento")
    @classmethod
    def validar_documento(cls, v: str | None) -> str | None:
        v = _texto_opcional(v)
        if v is not None and not re.match(r"^[A-Za-z0-9\-]{4,20}$", v):
            raise ValueError("El documento solo puede contener letras, números o guiones (entre 4 y 20 caracteres).")
        return v

    @field_validator("telefono")
    @classmethod
    def validar_telefono(cls, v: str | None) -> str | None:
        return _validar_telefono(v, obligatorio=False)

    @field_validator("direccion")
    @classmethod
    def limpiar_direccion(cls, v: str | None) -> str | None:
        return _texto_opcional(v)


class ItemDonacion(EsquemaBase):
    id_insumo: int = Field(..., gt=0)
    cantidad: Decimal = Field(..., gt=0, le=Decimal("99999999.99"), decimal_places=2)
    fecha_vencimiento: date | None = None

    @field_validator("fecha_vencimiento")
    @classmethod
    def no_vencido(cls, v: date | None) -> date | None:
        if v is not None and v < date.today():
            raise ValueError("No se puede ingresar un insumo con fecha de vencimiento pasada.")
        return v


class DonacionCrear(EsquemaBase):
    tipo_donacion: Literal["ESPECIE", "MONETARIA"]
    id_donante: int | None = Field(default=None, gt=0)
    monto_soles: Decimal | None = Field(default=None, ge=0, le=Decimal("99999999.99"), decimal_places=2)
    comprobante: str | None = Field(default=None, max_length=80)
    descripcion: str = Field(..., min_length=3, max_length=300)
    items: list[ItemDonacion] = Field(default_factory=list, max_length=100)

    @field_validator("tipo_donacion", mode="before")
    @classmethod
    def mayusculas(cls, v):
        return v.strip().upper() if isinstance(v, str) else v

    @field_validator("comprobante")
    @classmethod
    def limpiar_comprobante(cls, v: str | None) -> str | None:
        return _texto_opcional(v)

    @model_validator(mode="after")
    def coherencia_por_tipo(self) -> "DonacionCrear":
        if self.tipo_donacion == "MONETARIA":
            if self.monto_soles is None or self.monto_soles <= 0:
                raise ValueError("Para una donación monetaria debe indicar un monto en soles mayor a cero.")
            if self.items:
                raise ValueError("Una donación monetaria no puede incluir insumos. Regístrela como donación en especie.")
        else:
            self.monto_soles = None
        return self


# ---------------------------------------------------------------------------
# Inventario PEPS (HU-11)
# ---------------------------------------------------------------------------

class EntradaInventario(EsquemaBase):
    id_insumo: int = Field(..., gt=0)
    cantidad: Decimal = Field(..., gt=0, le=Decimal("99999999.99"), decimal_places=2)
    fecha_vencimiento: date | None = None
    motivo: str | None = Field(default=None, max_length=250)

    @field_validator("fecha_vencimiento")
    @classmethod
    def no_vencido(cls, v: date | None) -> date | None:
        if v is not None and v < date.today():
            raise ValueError("No se puede ingresar un insumo con fecha de vencimiento pasada.")
        return v


class SalidaInventario(EsquemaBase):
    id_insumo: int = Field(..., gt=0)
    cantidad: Decimal = Field(..., gt=0, le=Decimal("99999999.99"), decimal_places=2)
    motivo: str | None = Field(default=None, max_length=250)


class DescarteLote(EsquemaBase):
    id_lote: int = Field(..., gt=0)
    motivo: str = Field(..., min_length=5, max_length=250, description="Motivo obligatorio del descarte")


# ---------------------------------------------------------------------------
# Recetas (HU-12)
# ---------------------------------------------------------------------------

class DespacharReceta(EsquemaBase):
    id_receta: int = Field(..., gt=0)
    raciones: int = Field(..., gt=0, le=10000)


# ---------------------------------------------------------------------------
# Administración (HU-14)
# ---------------------------------------------------------------------------

class OllaEstadoAdmin(EsquemaBase):
    activada: bool


class UsuarioAdminUpdate(EsquemaBase):
    activo: bool | None = None
    nombre_completo: str | None = Field(default=None, min_length=3, max_length=150)
    telefono: str | None = None
    desbloquear: bool | None = Field(default=None, description="true = reinicia intentos fallidos y quita el bloqueo")

    @field_validator("telefono")
    @classmethod
    def validar_telefono(cls, v: str | None) -> str | None:
        return _validar_telefono(v, obligatorio=False)

    @model_validator(mode="after")
    def al_menos_un_campo(self) -> "UsuarioAdminUpdate":
        if not self.model_fields_set:
            raise ValueError("Debe enviar al menos un dato para actualizar el usuario.")
        return self
