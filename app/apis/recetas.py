"""HU-12: Calculadora de recetas locales, despacho atómico y recetario nutricional (API Ninjas)."""

import asyncio
import html
import logging
import re
import time
import unicodedata
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable

import httpx
import psycopg
from deep_translator import MyMemoryTranslator
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.config import settings
from app.core.database import consultar, ejecutar_sp, get_db
from app.schemas import DespacharReceta
from app.core.security import get_id_olla_contexto, get_usuario_actual

logger = logging.getLogger("ayni.recetas")
router = APIRouter(prefix="/api/recetas", tags=["Recetas"])

API_NINJAS_URL = "https://api.api-ninjas.com/v3/recipe"
CENTESIMA = Decimal("0.01")

SQL_RECETAS = """
SELECT r.id_receta, r.nombre, r.descripcion, r.raciones_base,
       ri.id_insumo, i.nombre AS insumo, i.unidad_base, ri.cantidad_base,
       ISNULL(s.stock_vigente, 0) AS stock_vigente
FROM dbo.recetas r
LEFT JOIN dbo.receta_ingredientes ri ON ri.id_receta = r.id_receta
LEFT JOIN dbo.insumos i ON i.id_insumo = ri.id_insumo
LEFT JOIN (
    SELECT id_insumo, SUM(cantidad_disponible) AS stock_vigente
    FROM dbo.lotes_inventario
    WHERE id_olla = ? AND estado = 'ACTIVO'
      AND (fecha_vencimiento IS NULL OR fecha_vencimiento >= CURRENT_DATE)
    GROUP BY id_insumo
) s ON s.id_insumo = ri.id_insumo
WHERE r.activa = 1
"""


def _cantidad_requerida(cantidad_base: Decimal, raciones: int, raciones_base: int) -> Decimal:
    """Misma fórmula que sp_DespacharRecetaAtomica (redondeo a 2 decimales, mínimo 0.01)."""
    valor = (Decimal(cantidad_base) * raciones / Decimal(raciones_base)).quantize(CENTESIMA, rounding=ROUND_HALF_UP)
    return max(valor, CENTESIMA)


def _armar_recetas(filas: list[dict[str, Any]], raciones: int | None) -> list[dict[str, Any]]:
    recetas: dict[int, dict[str, Any]] = {}
    for fila in filas:
        receta = recetas.setdefault(
            fila["id_receta"],
            {
                "id_receta": fila["id_receta"],
                "nombre": fila["nombre"],
                "descripcion": fila["descripcion"],
                "raciones_base": fila["raciones_base"],
                "raciones_calculadas": raciones or fila["raciones_base"],
                "ingredientes": [],
            },
        )
        if fila["id_insumo"] is None:
            continue
        requerida = _cantidad_requerida(fila["cantidad_base"], receta["raciones_calculadas"], fila["raciones_base"])
        stock = Decimal(fila["stock_vigente"])
        receta["ingredientes"].append(
            {
                "id_insumo": fila["id_insumo"],
                "insumo": fila["insumo"],
                "unidad_base": fila["unidad_base"],
                "cantidad_base": fila["cantidad_base"],
                "cantidad_requerida": requerida,
                "stock_vigente": stock,
                "alcanza": stock >= requerida,
                "faltante": max(requerida - stock, Decimal("0")),
            }
        )

    for receta in recetas.values():
        ingredientes = receta["ingredientes"]
        receta["se_puede_preparar"] = bool(ingredientes) and all(i["alcanza"] for i in ingredientes)
        if ingredientes:
            receta["raciones_maximas"] = int(
                min(i["stock_vigente"] * receta["raciones_base"] / Decimal(i["cantidad_base"]) for i in ingredientes)
            )
        else:
            receta["raciones_maximas"] = 0
    return list(recetas.values())


@router.get("", summary="Recetas activas con ingredientes, cálculo proporcional y stock vigente")
def listar_recetas(
    raciones: int | None = Query(default=None, gt=0, le=10000, description="Raciones a preparar (por defecto: raciones base)"),
    id_olla: int = Depends(get_id_olla_contexto),
    conn: psycopg.Connection = Depends(get_db),
):
    filas = consultar(conn, SQL_RECETAS + " ORDER BY r.id_receta, i.nombre", id_olla)
    return _armar_recetas(filas, raciones)


@router.post("/despachar", summary="Servir receta: descuenta todos los ingredientes por PEPS o revierte todo")
def despachar(
    datos: DespacharReceta,
    usuario: dict = Depends(get_usuario_actual),
    id_olla: int = Depends(get_id_olla_contexto),
    conn: psycopg.Connection = Depends(get_db),
):
    filas = consultar(conn, SQL_RECETAS + " AND r.id_receta = ? ORDER BY i.nombre", id_olla, datos.id_receta)
    if not filas:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="La receta seleccionada no existe o no está activa.")
    receta = _armar_recetas(filas, datos.raciones)[0]
    if not receta["ingredientes"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="La receta no tiene ingredientes configurados.")

    faltantes = [i for i in receta["ingredientes"] if not i["alcanza"]]
    if faltantes:
        detalle = ", ".join(f"{i['faltante']} {i['unidad_base']} de {i['insumo']}" for i in faltantes)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No hay stock vigente suficiente para preparar {datos.raciones} raciones de «{receta['nombre']}». "
            f"Falta: {detalle}. No se descontó ningún insumo. Puede preparar como máximo {receta['raciones_maximas']} raciones.",
        )

    resultado = ejecutar_sp(
        conn,
        "EXEC dbo.sp_DespacharRecetaAtomica @id_olla = ?, @id_receta = ?, @id_usuario = ?, @raciones = ?",
        id_olla,
        datos.id_receta,
        usuario["id_usuario"],
        datos.raciones,
    )
    return {
        "mensaje": f"Se sirvieron {datos.raciones} raciones de «{receta['nombre']}». "
        "Los ingredientes fueron descontados del almacén por PEPS.",
        **resultado,
        "ingredientes_descontados": [
            {"id_insumo": i["id_insumo"], "insumo": i["insumo"], "cantidad": i["cantidad_requerida"], "unidad_base": i["unidad_base"]}
            for i in receta["ingredientes"]
        ],
    }


# ---------------------------------------------------------------------------
# Recetario externo (API Ninjas /v3/recipe) con respaldo local
# ---------------------------------------------------------------------------

TRADUCCIONES = {
    "lentejas": "lentil soup",
    "lenteja": "lentil soup",
    "pollo": "chicken",
    "arroz": "rice",
    "papa": "potato",
    "papas": "potato",
    "atun": "tuna",
    "fideo": "pasta",
    "fideos": "pasta",
    "tallarin": "pasta",
    "tallarines": "pasta",
    "sopa": "soup",
    "frejol": "beans",
    "frejoles": "beans",
    "menestra": "beans",
    "huevo": "egg",
    "pescado": "fish",
    "verduras": "vegetables",
    "quinua": "quinoa",
    "avena": "oatmeal",
    "carne": "beef",
}
PALABRAS_VACIAS = {"de", "con", "y", "a", "al", "la", "el", "los", "las", "en", "para"}


def _sin_tildes(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def traducir_consulta(q: str) -> str:
    texto = _sin_tildes(q.lower().strip())
    if texto in TRADUCCIONES:
        return TRADUCCIONES[texto]
    palabras = [TRADUCCIONES.get(p, p) for p in texto.split() if p not in PALABRAS_VACIAS]
    # "sopa de pollo" -> "chicken soup"
    if "soup" in palabras and len(palabras) > 1:
        palabras = [p for p in palabras if p != "soup"] + ["soup"]
    resultado: list[str] = []
    for palabra in " ".join(palabras).split():
        if palabra not in resultado:
            resultado.append(palabra)
    return " ".join(resultado) or texto


RESPALDO_LOCAL: list[dict[str, Any]] = [
    {
        "claves": ["lentil", "soup", "beans"],
        "title": "Guiso de lentejas con arroz (receta comunitaria)",
        "servings": "50 raciones",
        "ingredients": ["4.5 kg de lentejas", "5 kg de arroz", "0.8 L de aceite vegetal", "3 cebollas grandes", "Ajo, comino y sal al gusto"],
        "instructions": "Remojar las lentejas 2 horas. Preparar un aderezo con cebolla, ajo y comino. Agregar las lentejas con agua y cocinar 40 minutos a fuego medio. Servir con arroz graneado.",
        "nutrition": {"calorias_kcal": 420, "proteinas_g": 18, "carbohidratos_g": 72, "grasas_g": 7, "hierro_mg": 5.2, "por": "ración"},
    },
    {
        "claves": ["chicken", "rice"],
        "title": "Arroz con pollo comunitario",
        "servings": "50 raciones",
        "ingredients": ["6 kg de arroz", "7 kg de pollo", "4 kg de papa", "1 L de aceite vegetal", "Culantro, arvejas y zanahoria"],
        "instructions": "Dorar las presas de pollo y reservar. Licuar culantro y preparar el aderezo. Añadir arroz, verduras y agua; cocinar tapado 20 minutos. Servir con el pollo y papa sancochada.",
        "nutrition": {"calorias_kcal": 560, "proteinas_g": 32, "carbohidratos_g": 70, "grasas_g": 14, "hierro_mg": 2.1, "por": "ración"},
    },
    {
        "claves": ["tuna", "potato"],
        "title": "Saltado de atún con papa y arroz",
        "servings": "50 raciones",
        "ingredients": ["25 latas de atún", "6 kg de papa", "5.5 kg de arroz", "0.7 L de aceite vegetal", "Cebolla, tomate y sillao"],
        "instructions": "Freír la papa en bastones. Saltear cebolla y tomate, agregar el atún escurrido y sazonar. Mezclar con la papa y servir con arroz.",
        "nutrition": {"calorias_kcal": 480, "proteinas_g": 24, "carbohidratos_g": 66, "grasas_g": 12, "hierro_mg": 1.8, "por": "ración"},
    },
    {
        "claves": ["pasta", "soup"],
        "title": "Sopa de fideos con verduras",
        "servings": "50 raciones",
        "ingredients": ["4 kg de fideo", "5 kg de papa", "3 kg de zanahoria", "2 kg de zapallo", "0.5 L de aceite vegetal"],
        "instructions": "Hervir agua con las verduras picadas por 20 minutos. Añadir el fideo y la papa en cubos; cocinar 12 minutos más. Sazonar y servir caliente.",
        "nutrition": {"calorias_kcal": 350, "proteinas_g": 10, "carbohidratos_g": 64, "grasas_g": 6, "hierro_mg": 1.5, "por": "ración"},
    },
]


def _respaldo(query_en: str) -> list[dict[str, Any]]:
    palabras = set(query_en.split())
    coincidencias = [r for r in RESPALDO_LOCAL if palabras & set(r["claves"])] or RESPALDO_LOCAL
    return [{k: v for k, v in r.items() if k != "claves"} for r in coincidencias]


def _ingrediente_a_texto(valor: Any) -> str:
    """v3 devuelve {"name", "quantity", "unit"}; v1 devolvía texto plano."""
    if isinstance(valor, dict):
        cantidad = valor.get("quantity")
        unidad = valor.get("unit")
        if unidad in (None, "", "unit"):
            unidad = ""
        partes = [str(cantidad) if cantidad not in (None, "") else "", str(unidad), str(valor.get("name") or "")]
        return " ".join(p for p in partes if p).strip()
    return str(valor).strip()


def _como_lista(valor: Any) -> list[str]:
    if valor is None:
        return []
    if isinstance(valor, list):
        return [t for t in (_ingrediente_a_texto(v) for v in valor) if t]
    return [p.strip() for p in str(valor).split("|") if p.strip()]


def _normalizar(item: dict[str, Any]) -> dict[str, Any]:
    instrucciones = item.get("instructions")
    pasos = [str(p).strip() for p in instrucciones if str(p).strip()] if isinstance(instrucciones, list) else (
        [instrucciones.strip()] if isinstance(instrucciones, str) and instrucciones.strip() else []
    )
    nutricion = item.get("nutrition") or item.get("nutrition_info")
    nota = None
    if nutricion is not None and not isinstance(nutricion, (dict, list)):
        # El plan gratuito de API Ninjas devuelve un texto en lugar de los valores nutricionales.
        nota = "La información nutricional de esta receta no está disponible en el plan actual de API Ninjas."
        nutricion = None
    return {
        "title": item.get("title") or item.get("name"),
        "servings": str(item["servings"]) if item.get("servings") not in (None, "") else None,
        "ingredients": _como_lista(item.get("ingredients")),
        "instructions": " ".join(pasos) or None,
        "pasos": pasos,
        "nutrition": nutricion,
        "nutrition_nota": nota,
    }


# ---------------------------------------------------------------------------
# Traducción automática inglés → español con cadena de proveedores
#
#   1. Google Cloud Translation API (oficial) — solo si GOOGLE_TRANSLATE_API_KEY
#      está configurada; traduce listas completas en una sola petición.
#   2. Google Translate gratuito (endpoint público translate.googleapis.com,
#      client=gtx, vía httpx) — sin clave, rápido y conserva los saltos de
#      línea. (deep-translator usaba otra URL que Google bloquea siempre con
#      TooManyRequests, por eso ya no se usa.)
#   3. MyMemory (deep-translator) — gratuito, sin clave, máx. ~500 caracteres
#      por petición y ~5000 caracteres al día por IP; si agotó su cuota, su
#      aviso se detecta y no se muestra como "traducción".
#   4. Si todos fallan: el texto queda en inglés (se registra en el log).
#
# Si un proveedor falla a mitad de camino, lo ya traducido se conserva y el
# siguiente proveedor solo traduce lo que faltaba.
#
# Las porciones ("1 Servings") se convierten siempre con una regla propia, sin
# depender de ningún proveedor. Solo se guardan en caché las búsquedas que se
# tradujeron por completo, para reintentar las que fallaron.
# ---------------------------------------------------------------------------

_CACHE_RECETAS: dict[str, dict[str, Any]] = {}
_CACHE_MAXIMO = 100
_GOOGLE_CLOUD_URL = "https://translation.googleapis.com/language/translate/v2"
_GOOGLE_GTX_URL = "https://translate.googleapis.com/translate_a/single"
_PAUSA_TRAS_BLOQUEO = 120  # segundos sin usar un proveedor tras un fallo
_bloqueado_hasta: dict[str, float] = {}

_RE_SERVINGS = re.compile(r"^\s*(\d+(?:\s*[-–]\s*\d+)?)\s*(servings?|portions?|people|persons?)?\s*$", re.IGNORECASE)


def _servings_es(valor: Any) -> str | None:
    """Normaliza `servings` de API Ninjas: 1 / "1 Servings" -> "1 porción"; "4 servings" -> "4 porciones".

    Devuelve None si el valor no sigue el patrón numérico (entonces se traduce como texto).
    """
    if valor is None or isinstance(valor, bool):
        return None
    coincidencia = _RE_SERVINGS.match(str(valor))
    if not coincidencia:
        return None
    numero = re.sub(r"\s+", "", coincidencia.group(1))
    return f"{numero} {'porción' if numero == '1' else 'porciones'}"


def _disponible(proveedor: str) -> bool:
    return time.monotonic() >= _bloqueado_hasta.get(proveedor, 0.0)


def _marcar_bloqueo(proveedor: str, exc: Exception) -> None:
    logger.warning(
        "El traductor %s falló (%s: %s); se omite durante %d s.",
        proveedor, type(exc).__name__, str(exc)[:160], _PAUSA_TRAS_BLOQUEO,
    )
    _bloqueado_hasta[proveedor] = time.monotonic() + _PAUSA_TRAS_BLOQUEO


def _google_cloud(textos: list[str]) -> list[str]:
    """API oficial de Google (v2): una sola petición para toda la lista."""
    respuesta = httpx.post(
        _GOOGLE_CLOUD_URL,
        params={"key": settings.google_translate_api_key},
        json={"q": textos, "source": "en", "target": "es", "format": "text"},
        timeout=15.0,
    )
    respuesta.raise_for_status()
    traducciones = respuesta.json()["data"]["translations"]
    return [html.unescape(t["translatedText"]).strip() for t in traducciones]


def _en_lotes(textos: list[str], limite: int) -> list[list[str]]:
    """Agrupa textos en lotes cuyo total (unido con saltos de línea) no supere `limite`."""
    lotes: list[list[str]] = []
    actual: list[str] = []
    largo = 0
    for texto in textos:
        if actual and largo + len(texto) + 1 > limite:
            lotes.append(actual)
            actual, largo = [], 0
        actual.append(texto)
        largo += len(texto) + 1
    if actual:
        lotes.append(actual)
    return lotes


def _google_gtx(texto: str, origen: str = "en", destino: str = "es") -> str:
    """Google Translate gratuito (client=gtx). Respeta los saltos de línea del texto."""
    respuesta = httpx.post(
        _GOOGLE_GTX_URL,
        params={"client": "gtx", "sl": origen, "tl": destino, "dt": "t"},
        data={"q": texto},
        timeout=10.0,
    )
    respuesta.raise_for_status()
    segmentos = respuesta.json()[0] or []
    return "".join(s[0] for s in segmentos if s and s[0])


def _mymemory(texto: str) -> str:
    traducido = MyMemoryTranslator(source="en-GB", target="es-ES").translate(texto) or ""
    if "MYMEMORY WARNING" in traducido.upper() or "QUERY LENGTH LIMIT" in traducido.upper():
        raise RuntimeError("MyMemory agotó su cuota gratuita diaria")
    return traducido


def _por_lotes(traducir: Callable[[str], str], textos: list[str], limite: int, avance: list[str]) -> list[str]:
    """Traduce por lotes unidos con '\\n'; si un lote pierde líneas, lo traduce ítem por ítem.

    Cada lote terminado se agrega a `avance`, así un fallo a mitad de camino no
    pierde lo que ya se tradujo.
    """
    for lote in _en_lotes(textos, limite):
        unidas = (traducir("\n".join(lote)) or "").strip()
        lineas = [linea.strip() for linea in unidas.split("\n") if linea.strip()]
        if len(lineas) != len(lote):
            lineas = [(traducir(t[:limite]) or "").strip() for t in lote]
        if not all(lineas):
            raise ValueError("la traducción devolvió textos vacíos")
        avance.extend(lineas)
    return avance


def _proveedores() -> list[tuple[str, Callable[[list[str], list[str]], list[str]]]]:
    cadena: list[tuple[str, Callable[[list[str], list[str]], list[str]]]] = []
    if settings.google_translate_api_key:
        cadena.append(("Google Cloud", lambda textos, avance: avance + _google_cloud(textos)))
    cadena.append(("Google gratuito", lambda textos, avance: _por_lotes(_google_gtx, textos, 4500, avance)))
    cadena.append(("MyMemory", lambda textos, avance: _por_lotes(_mymemory, textos, 450, avance)))
    return cadena


def traducir_lista(textos: list[str]) -> tuple[list[str], bool]:
    """Traduce una lista inglés → español con la cadena de proveedores.

    Devuelve (textos, traducido). Si ningún proveedor responde, devuelve los
    textos originales y `traducido=False` (sin lanzar excepciones).
    """
    limpios = [t.strip() for t in textos]
    if not any(limpios):
        return limpios, True
    hechos: list[str] = []  # traducciones ya obtenidas, en orden
    for nombre, funcion in _proveedores():
        if not _disponible(nombre):
            continue
        avance: list[str] = []
        try:
            funcion(limpios[len(hechos):], avance)
            hechos.extend(avance)
            if len(hechos) == len(limpios):
                return [h.replace("\u00a0", " ").replace("\u202f", " ") for h in hechos], True
            raise ValueError("la traducción no devolvió el mismo número de textos")
        except Exception as exc:  # noqa: BLE001 - se prueba el siguiente proveedor
            hechos.extend(avance[: len(limpios) - len(hechos)])
            _marcar_bloqueo(nombre, exc)
    logger.warning(
        "Ningún traductor completó la tarea; %d de %d texto(s) quedan en inglés.",
        len(limpios) - len(hechos), len(limpios),
    )
    return hechos + limpios[len(hechos):], False


def _traducir_buscada(q: str) -> str:
    """Español → inglés de la búsqueda con Google gratuito (gtx); si falla, usa el diccionario local."""
    if _disponible("Google gratuito"):
        try:
            traducido = _google_gtx(q.strip(), origen="es", destino="en")
            if traducido and traducido.strip():
                return traducido.strip()
        except Exception as exc:  # noqa: BLE001 - cualquier fallo usa el traductor local
            _marcar_bloqueo("Google gratuito", exc)
    return traducir_consulta(q)


def _traducir_recetas(recetas: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Traduce al español `title`, `servings`, `ingredients` y `pasos`/`instructions`
    de todas las recetas en una sola tanda, campo por campo.

    Devuelve (recetas, todo_traducido). Si algo falla, ese campo queda en inglés
    pero el resto sí se traduce; las porciones se convierten siempre.
    """
    textos: list[str] = []
    destinos: list[tuple[int, str, int | None]] = []  # (receta, campo, posición en la lista)
    salida = [dict(r) for r in recetas]

    for i, receta in enumerate(salida):
        if receta.get("title"):
            destinos.append((i, "title", None))
            textos.append(str(receta["title"]))
        servings = receta.get("servings")
        if servings not in (None, ""):
            fijo = _servings_es(servings)
            if fijo:
                receta["servings"] = fijo
            else:
                destinos.append((i, "servings", None))
                textos.append(str(servings))
        for j, ingrediente in enumerate(receta.get("ingredients") or []):
            destinos.append((i, "ingredients", j))
            textos.append(str(ingrediente))
        pasos = receta.get("pasos") or []
        if pasos:
            receta["pasos"] = list(pasos)
            for j, paso in enumerate(pasos):
                destinos.append((i, "pasos", j))
                textos.append(str(paso))
        elif receta.get("instructions"):
            destinos.append((i, "instructions", None))
            textos.append(str(receta["instructions"]))
        if receta.get("ingredients"):
            receta["ingredients"] = list(receta["ingredients"])

    traducidos, ok = traducir_lista(textos)
    for (i, campo, j), texto in zip(destinos, traducidos):
        if j is None:
            salida[i][campo] = texto
        else:
            salida[i][campo][j] = texto
    for receta in salida:
        if receta.get("pasos"):
            receta["instructions"] = " ".join(receta["pasos"])
        receta["traducido"] = ok
    return salida, ok


@router.get("/externas", summary="Sugerencias de recetas con información nutricional (API Ninjas)")
async def recetas_externas(
    q: str = Query(..., min_length=2, max_length=80, description="Ej.: lentejas, pollo, arroz, papa, atún, fideo, sopa"),
    _: dict = Depends(get_usuario_actual),
):
    # Búsqueda repetida: se responde al instante desde la caché en memoria.
    clave = _sin_tildes(q.lower().strip())
    if clave in _CACHE_RECETAS:
        return {**_CACHE_RECETAS[clave], "consulta": q}

    api_key = settings.api_ninjas_key
    aviso = None

    if api_key:
        # 1) Español → inglés de la búsqueda antes de consultar API Ninjas.
        query_en = await asyncio.to_thread(_traducir_buscada, q)
        try:
            async with httpx.AsyncClient(timeout=8.0) as cliente:
                respuesta = await cliente.get(API_NINJAS_URL, params={"title": query_en}, headers={"X-Api-Key": api_key})
            respuesta.raise_for_status()
            datos = respuesta.json()
            if isinstance(datos, dict):
                datos = datos.get("results") or datos.get("data") or datos.get("recipes") or [datos]
            recetas = [_normalizar(r) for r in datos if isinstance(r, dict)]
            recetas = [r for r in recetas if r["title"]]
            if recetas:
                # 2) Inglés → español de todas las recetas (si un proveedor falla, se usa el siguiente).
                recetas, traducido = await asyncio.to_thread(_traducir_recetas, recetas)
                salida = {
                    "consulta": q, "consulta_en": query_en, "fuente": "API Ninjas",
                    "traducido": traducido, "total": len(recetas), "recetas": recetas,
                }
                if not traducido:
                    salida["aviso"] = "El servicio de traducción no respondió; algunos textos se muestran en inglés."
                else:
                    # Solo se cachean búsquedas traducidas por completo.
                    if len(_CACHE_RECETAS) >= _CACHE_MAXIMO:
                        _CACHE_RECETAS.pop(next(iter(_CACHE_RECETAS)))  # la más antigua
                    _CACHE_RECETAS[clave] = {k: v for k, v in salida.items() if k != "consulta"}
                return salida
            aviso = "API Ninjas no encontró recetas para su búsqueda. Se muestran sugerencias locales."
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Fallo al consultar API Ninjas: %s", exc)
            aviso = "No se pudo conectar con el recetario externo. Se muestran sugerencias locales."
    else:
        query_en = traducir_consulta(q)
        aviso = "No se configuró API_NINJAS_KEY. Se muestran sugerencias locales."

    recetas = _respaldo(query_en)
    return {
        "consulta": q,
        "consulta_en": query_en,
        "fuente": "respaldo_local",
        "aviso": aviso,
        "total": len(recetas),
        "recetas": recetas,
    }
