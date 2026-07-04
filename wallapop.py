"""
Módulo de precios de mercado via Wallapop API.
- Agrupa coches por generación (evita búsquedas duplicadas)
- Caché diario por generación
- Delay aleatorio entre llamadas
- Devuelve URL del anuncio de referencia más barato
"""
from __future__ import annotations

import json
import time
import random
import logging
import requests
from datetime import date
from pathlib import Path

logger = logging.getLogger("auto1_scraper")

GENERATIONS_FILE = Path(__file__).parent / "model_generations.json"
CACHE_FILE = Path(__file__).parent / "logs" / "wallapop_cache.json"

WALLAPOP_API = "https://api.wallapop.com/api/v3/cars/search"
HEADERS = {
    "User-Agent": "Wallapop/23.75.0 (iPhone; iOS 16.0; Scale/3.0)",
    "Accept": "application/json",
    "Accept-Language": "es-ES,es;q=0.9",
    "DeviceOS": "0",
}

# ─────────────────────────────────────────
# Generaciones
# ─────────────────────────────────────────

def _cargar_generaciones() -> dict:
    with open(GENERATIONS_FILE, "r") as f:
        return json.load(f)


def obtener_generacion(marca: str, modelo: str, año: int) -> dict | None:
    generaciones = _cargar_generaciones()
    marca_key = marca.lower().strip()
    modelo_key = modelo.lower().strip()
    marca_data = generaciones.get(marca_key, {})
    modelo_data = marca_data.get(modelo_key)
    if not modelo_data:
        return None
    for gen in modelo_data:
        inicio = gen["years"][0]
        fin = gen["years"][1] if gen["years"][1] is not None else 9999
        if inicio <= año <= fin:
            return gen
    return None


def clave_busqueda(marca: str, modelo: str, año: int) -> tuple:
    gen = obtener_generacion(marca, modelo, año)
    if gen:
        return (marca.lower(), modelo.lower(), gen["years"][0], gen["years"][1] or 9999, gen["gen"])
    else:
        return (marca.lower(), modelo.lower(), año - 1, año + 1, "fallback")


# ─────────────────────────────────────────
# Caché diario
# ─────────────────────────────────────────

def _cargar_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    with open(CACHE_FILE, "r") as f:
        data = json.load(f)
    hoy = str(date.today())
    return {k: v for k, v in data.items() if v.get("fecha") == hoy}


def _guardar_cache(cache: dict):
    CACHE_FILE.parent.mkdir(exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────
# Llamada a Wallapop
# ─────────────────────────────────────────

_MARCA_MAP = {
    "mercedes-benz": "mercedes",
    "volkswagen": "volkswagen",
    "bmw": "bmw",
    "audi": "audi",
    "seat": "seat",
    "skoda": "skoda",
    "renault": "renault",
    "peugeot": "peugeot",
    "citroen": "citroen",
    "opel": "opel",
    "ford": "ford",
    "toyota": "toyota",
    "hyundai": "hyundai",
    "kia": "kia",
    "honda": "honda",
    "nissan": "nissan",
    "fiat": "fiat",
    "land rover": "land rover",
    "alfa romeo": "alfa romeo",
}


def _normalizar_marca(marca: str) -> str:
    """Normaliza nombres de marca para búsqueda en Wallapop."""
    return _MARCA_MAP.get(marca.lower().strip(), marca.lower().strip())


def _normalizar_modelo(modelo: str) -> str:
    """Elimina sufijos de generación del nombre de modelo."""
    # Eliminar sufijos tipo "VIII", "VII", etc. que Auto1 incluye en el modelo
    import re
    modelo = re.sub(r'\s+(I{1,3}|IV|V?I{0,3}|VIII|IX|X)$', '', modelo.strip())
    return modelo.strip()


def _construir_url_wallapop(item: dict) -> str:
    """Construye la URL pública del anuncio de Wallapop."""
    slug = item.get("web_slug") or item.get("slug")
    item_id = item.get("id") or item.get("itemId")
    if slug:
        return f"https://es.wallapop.com/item/{slug}"
    elif item_id:
        return f"https://es.wallapop.com/item/{item_id}"
    return ""


def _buscar_wallapop(marca: str, modelo: str, año_desde: int, año_hasta: int, km_max: int = 200000) -> list:
    """
    Usa Playwright headless para interceptar la respuesta de la API de Wallapop.
    Más robusto que llamada directa (requiere autenticación interna del browser).
    """
    from playwright.sync_api import sync_playwright
    import urllib.parse

    marca_norm = _normalizar_marca(marca)
    modelo_norm = _normalizar_modelo(modelo)

    keywords = urllib.parse.quote(f"{marca_norm} {modelo_norm}")
    url_busqueda = (
        f"https://es.wallapop.com/app/search?keywords={keywords}"
        f"&category_ids=100"
        f"&filters_source=search_box"
        f"&latitude=40.4168&longitude=-3.7038"
        f"&order_by=price_low_to_high"
        f"&min_year={año_desde}&max_year={año_hasta}"
        f"&max_km={km_max}"
        f"&min_sale_price=500&max_sale_price=70000"
    )

    captured = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        def on_response(response):
            if "search/section" in response.url and "wallapop" in response.url and response.status == 200:
                if not captured:
                    try:
                        data = response.json()
                        items = data.get("data", {}).get("section", {}).get("items", [])
                        if items is not None:
                            captured["items"] = items
                    except Exception:
                        pass

        page.on("response", on_response)

        try:
            page.goto(url_busqueda, timeout=25000)
            for _ in range(24):
                if captured:
                    break
                page.wait_for_timeout(500)
        except Exception as e:
            logger.warning(f"⚠️  Timeout cargando Wallapop: {e}")
        finally:
            browser.close()

    items = captured.get("items", [])
    resultados = []
    for item in items:
        # Estructura del endpoint /search/section
        precio_raw = item.get("price", {})
        if isinstance(precio_raw, dict):
            precio = precio_raw.get("amount")
        else:
            precio = precio_raw

        titulo = item.get("title", "")
        slug = item.get("web_slug", "")
        url = f"https://es.wallapop.com/item/{slug}" if slug else ""

        # Año y km están en type_attributes
        attrs = item.get("type_attributes", {})
        año_item = attrs.get("year") or attrs.get("registration_year")
        km_item = attrs.get("km") or attrs.get("mileage")

        if precio and precio > 500:
            resultados.append({
                "titulo": titulo,
                "precio": float(precio),
                "año": int(año_item) if año_item else None,
                "km": int(km_item) if km_item else None,
                "url": url,
            })

    logger.info(f"   → {len(resultados)} anuncios encontrados en Wallapop")
    return resultados


# ─────────────────────────────────────────
# Precio de mercado
# ─────────────────────────────────────────

def _construir_url_busqueda(marca: str, modelo: str, año_desde: int, año_hasta: int, km: int) -> str:
    """Construye la URL de búsqueda en Wallapop con filtros aplicados, ordenada por precio."""
    import urllib.parse
    marca_norm = _normalizar_marca(marca)
    modelo_norm = _normalizar_modelo(modelo)
    keywords = urllib.parse.quote(f"{marca_norm} {modelo_norm}")
    km_min = max(0, km - 20000)
    km_max = km + 20000
    return (
        f"https://es.wallapop.com/app/search?keywords={keywords}"
        f"&category_ids=100"
        f"&filters_source=search_box"
        f"&latitude=40.4168&longitude=-3.7038"
        f"&order_by=price_low_to_high"
        f"&min_year={año_desde}&max_year={año_hasta}"
        f"&min_km={km_min}&max_km={km_max}"
        f"&min_sale_price=500&max_sale_price=70000"
    )


def precio_mercado(marca: str, modelo: str, año: int, km: int, cache: dict) -> dict:
    """
    Obtiene el precio de mercado en Wallapop para un coche concreto.
    Devuelve precio medio de los 3 más baratos y URL de búsqueda filtrada.
    """
    clave = clave_busqueda(marca, modelo, año)
    clave_str = "|".join(str(x) for x in clave)
    gen = obtener_generacion(marca, modelo, año)

    # Comprobar caché
    if clave_str in cache:
        logger.info(f"   📦 Wallapop caché: {marca} {modelo} gen {clave[4]}")
        anuncios = cache[clave_str]["anuncios"]
        fuente = "cache"
    else:
        año_desde = clave[2]
        año_hasta = clave[3] if clave[3] != 9999 else año + 1

        logger.info(f"   🔍 Wallapop API: {marca} {modelo} ({año_desde}-{año_hasta})")
        anuncios = _buscar_wallapop(marca, modelo, año_desde, año_hasta)

        cache[clave_str] = {
            "fecha": str(date.today()),
            "anuncios": anuncios,
        }
        _guardar_cache(cache)

        delay = random.uniform(8, 15)
        logger.info(f"   ⏳ Esperando {delay:.1f}s...")
        time.sleep(delay)
        fuente = "api"

    año_desde = clave[2]
    año_hasta = clave[3] if clave[3] != 9999 else año + 1
    url_busqueda = _construir_url_busqueda(marca, modelo, año_desde, año_hasta, km)

    # Filtrar anuncios con km claramente erróneos (< 1000 km son entradas mal introducidas en Wallapop)
    anuncios_validos = [
        a for a in anuncios
        if a.get("km") is None or a["km"] >= 1000
    ]
    if not anuncios_validos:
        anuncios_validos = anuncios

    # Filtrar por km del coche: ±20.000 km respecto al coche de Auto1
    km_min = max(0, km - 20000)
    km_max = km + 20000
    candidatos = [
        a for a in anuncios_validos
        if a.get("km") is not None and km_min <= a["km"] <= km_max
    ]
    if not candidatos:
        # Fallback: ampliar a ±40.000 km si no hay suficientes
        candidatos = [
            a for a in anuncios_validos
            if a.get("km") is not None and max(0, km - 40000) <= a["km"] <= km + 40000
        ]
    if not candidatos:
        candidatos = anuncios_validos

    candidatos_sorted = sorted(candidatos, key=lambda x: x["precio"])
    top3 = candidatos_sorted[:3]

    if not top3:
        return {
            "precio_medio": None,
            "n_anuncios": 0,
            "generacion": gen["gen"] if gen else "desconocida",
            "fiable": False,
            "fuente": fuente,
            "ref_titulo": "",
            "ref_precio": None,
            "ref_url": "",
        }

    precio_medio = round(sum(a["precio"] for a in top3) / len(top3))
    ref = top3[0]  # El más barato del top3 como referencia de precio

    return {
        "precio_medio": precio_medio,
        "n_anuncios": len(anuncios),
        "top3": top3,
        "generacion": gen["gen"] if gen else "fallback ±1año",
        "fiable": len(top3) >= 3,
        "fuente": fuente,
        "ref_titulo": ref.get("titulo", ""),
        "ref_precio": ref.get("precio"),
        "ref_url": url_busqueda,  # URL de búsqueda filtrada y ordenada por precio
        "ref_listing_url": ref.get("url", ""),  # URL directa al anuncio más barato
    }


# ─────────────────────────────────────────
# Enriquecer lista de coches con precios
# ─────────────────────────────────────────

def enriquecer_con_precios_mercado(coches: list) -> list:
    """
    Añade precio_mercado, margen, rentabilidad % y URL de referencia Wallapop a cada coche.
    """
    cache = _cargar_cache()
    resultado = []

    for coche in coches:
        marca = coche.get("marca", "")
        modelo = coche.get("modelo", "")
        año = coche.get("año") or 0
        km = coche.get("km") or 0
        precio_auto1 = coche.get("precio_auto1") or 0

        if not marca or not modelo or not año:
            coche["precio_mercado_wallapop"] = None
            coche["wallapop_ref_titulo"] = ""
            coche["wallapop_ref_precio"] = None
            coche["wallapop_ref_url"] = ""
            coche["wallapop_listing_url"] = ""
            resultado.append(coche)
            continue

        mercado = precio_mercado(marca, modelo, año, km, cache)

        coche["precio_mercado_wallapop"] = mercado.get("precio_medio")
        coche["mercado_n_anuncios"] = mercado.get("n_anuncios", 0)
        coche["mercado_generacion"] = mercado.get("generacion")
        coche["mercado_fiable"] = mercado.get("fiable", False)
        coche["wallapop_ref_titulo"] = mercado.get("ref_titulo", "")
        coche["wallapop_ref_precio"] = mercado.get("ref_precio")
        coche["wallapop_ref_url"] = mercado.get("ref_url", "")
        coche["wallapop_listing_url"] = mercado.get("ref_listing_url", "")

        resultado.append(coche)

    return resultado
