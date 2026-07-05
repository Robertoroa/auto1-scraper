"""
Módulo de precios de mercado via coches.net.
- Filtra solo anuncios "Super precio" (priceRankIndicator == 1)
- Excluye coches con averías declaradas
- Lógica "precio techo": acepta coches iguales o MEJORES (mismo año o más nuevo,
  km hasta +20%) y descarta solo los claramente peores (mucho más viejos / más km)
- Precio final = el más barato REALISTA (descarta precios ridículos: >30% por
  debajo del siguiente anuncio)
- URL pattern para furgonetas: /{marca}/{modelo}/vehiculos-industriales/
- URL pattern para coches:     /{marca}/{modelo}/segunda-mano/
- Extrae datos del script __INITIAL_PROPS__ (JSON escapado dentro del HTML)
- Caché diario por clave marca/modelo/año_grupo
"""
from __future__ import annotations

import json
import re
import time
import random
import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger("auto1_scraper")

CACHE_FILE = Path(__file__).parent / "logs" / "cochesnet_cache.json"

MIN_SUPER_PRECIO = 2   # mínimo de anuncios "Super precio" limpios para considerar el dato fiable
GAP_OUTLIER      = 0.30  # si el más barato está >30% por debajo del siguiente, se considera precio ridículo y se pasa al siguiente

# Tolerancias para filtrar por año y km — LÓGICA DE "PRECIO TECHO".
# Un coche MÁS NUEVO o con MENOS km siempre es comparable (es igual o mejor que el
# nuestro): si además es más barato, marca el techo de precio y el nuestro no puede
# valer más. Solo se descartan los claramente PEORES: mucho más viejos o con
# muchos más km que el nuestro.
AÑO_MAX_ANTIGUEDAD = 3      # se aceptan coches hasta 3 años más viejos; más nuevos, sin límite
KM_MAX_FACTOR      = 1.20   # máx km = km_objetivo * 1.20 (20% más); menos km, sin límite
KM_MAX_ABSOLUTO    = 250_000  # tope duro si no conocemos los km del coche objetivo

# Campos de coches.net que indican daños o avería
_CAMPOS_DANO = [
    "mechanicalDamage",
    "bodyDamage",
    "accidents",
    "hasAccidents",
    "damaged",
    "isDamaged",
]

# ─────────────────────────────────────────
# Caché diario
# ─────────────────────────────────────────

def _cargar_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
        hoy = str(date.today())
        return {k: v for k, v in data.items() if v.get("fecha") == hoy}
    except Exception:
        return {}


def _guardar_cache(cache: dict):
    CACHE_FILE.parent.mkdir(exist_ok=True)
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────
# Slugs para URLs de coches.net
# ─────────────────────────────────────────

_SUFIJOS_VARIANTE = [
    " cargo", " van", " combi", " panel", " crew", " platform",
    " cargo l1", " cargo l2", " cargo l3",
    " talla m", " talla xl",
]

_MODELO_FIXES = {
    "transit custom": "transit_custom",
    "transit connect": "transit_connect",
    "berlingo first": "berlingo_first",
    "space tourer": "space_tourer",
    "grand voyager": "grand_voyager",
    "grand scenic": "grand_scenic",
    "c-max": "c-max",
    "b-max": "b-max",
    "s-max": "s-max",
}

# Fixes por combinación marca+modelo (clave: "marca|modelo" en minúsculas)
_MARCA_MODELO_FIXES = {
    "mini|cabrio": "mini-cabrio",  # coches.net usa mini/mini-cabrio/
}

_MARCA_FIXES = {
    "mercedes-benz": "mercedes-benz",
    "vw": "volkswagen",
    "alfa romeo": "alfa-romeo",
    "land rover": "land-rover",
    "aston martin": "aston-martin",
}


def _marca_slug(marca: str) -> str:
    m = marca.lower().strip()
    return _MARCA_FIXES.get(m, m.replace(" ", "-"))


def _modelo_slug(modelo: str) -> str:
    m = modelo.lower().strip()
    if m in _MODELO_FIXES:
        return _MODELO_FIXES[m]
    for sufijo in _SUFIJOS_VARIANTE:
        if m.endswith(sufijo):
            m = m[: -len(sufijo)].strip()
            break
    if m in _MODELO_FIXES:
        return _MODELO_FIXES[m]
    m = m.replace(" ", "_")
    m = re.sub(r"[^a-z0-9_\-]", "", m)
    return m


def _construir_urls(marca: str, modelo: str, año: int) -> list:
    ms = _marca_slug(marca)
    ml = _modelo_slug(modelo)
    # Fix por combinación marca+modelo
    combo_key = f"{ms}|{ml}"
    ml = _MARCA_MODELO_FIXES.get(combo_key, ml)
    return [
        f"https://www.coches.net/{ms}/{ml}/segunda-mano/",
        f"https://www.coches.net/{ms}/{ml}/vehiculos-industriales/",
        f"https://www.coches.net/{ms}/vehiculos-industriales/",
    ]


# ─────────────────────────────────────────
# Parseo del JSON embebido en el script
# ─────────────────────────────────────────

_JS_EXTRACTOR = """
() => {
    const scripts = Array.from(document.scripts);
    const script = scripts.find(s => s.textContent.includes('priceAverageIndicator'));
    if (!script) return {found: false, text: '', title: document.title};
    return {found: true, text: script.textContent, title: document.title};
}
"""


def _parsear_items(script_text: str) -> list:
    """
    Extrae la lista de items del JSON escapado en __INITIAL_PROPS__.
    El formato es: window.__INITIAL_PROPS__ = JSON.parse("...escaped json...")
    """
    idx = script_text.find('JSON.parse(')
    if idx < 0:
        return []
    try:
        # Localizar la apertura de comillas del string escapado
        q_idx = script_text.index('"', idx)
        # Leer el string escapado carácter a carácter
        raw = []
        i = q_idx + 1
        while i < len(script_text):
            c = script_text[i]
            if c == '\\':
                raw.append(script_text[i:i + 2])
                i += 2
            elif c == '"':
                break
            else:
                raw.append(c)
                i += 1
        escaped = ''.join(raw)
        data_str = json.loads('"' + escaped + '"')
        parsed = json.loads(data_str)
        items = parsed.get('initialResults', {}).get('items', [])
        return items if isinstance(items, list) else []
    except Exception as e:
        logger.debug(f"   coches.net: error parseando items JSON: {e}")
        return []


def _tiene_dano(item: dict) -> bool:
    """Devuelve True si el anuncio declara algún tipo de daño o avería."""
    for campo in _CAMPOS_DANO:
        val = item.get(campo)
        if val is True or val == "true" or val == 1:
            return True
    return False


def _filtrar_items(items: list, año_obj: int, km_obj: int) -> list:
    """
    Filtra items por condición base (sin averías, año/km similares).
    NO filtra por rank — eso se hace después según el plan A/B/C.
    """
    resultado = []
    for item in items:
        # Sin daños declarados
        if _tiene_dano(item):
            continue

        # Filtro por año (precio techo): descarta solo los claramente MÁS VIEJOS.
        # Un coche igual o más nuevo que el nuestro siempre cuenta como comparable.
        año_item = item.get("year") or item.get("registrationYear") or 0
        if año_obj and año_item:
            if int(año_item) < int(año_obj) - AÑO_MAX_ANTIGUEDAD:
                continue

        # Filtro por km (precio techo): descarta solo los que tienen MUCHOS más km.
        # Menos km que el nuestro siempre cuenta (es mejor coche).
        km_item = item.get("km") or item.get("mileage") or 0
        if km_item > KM_MAX_ABSOLUTO:
            continue
        if km_obj and km_obj > 0 and km_item > km_obj * KM_MAX_FACTOR:
            continue

        precio = item.get("price") or 0
        if precio < 500:
            continue

        resultado.append(item)

    return resultado


def _precio_mas_barato_realista(items_filtrados: list, gap: float = GAP_OUTLIER) -> int | None:
    """
    Devuelve el precio más barato REALISTA de la lista.

    Ordena los precios de menor a mayor y coge el más barato, salvo que esté
    "descolgado": si un precio es más de `gap` (30%) por debajo del siguiente,
    se considera un precio ridículo (error, siniestro no declarado, estafa…) y
    se pasa al siguiente. Se repite hacia arriba hasta el primero no descolgado.
    """
    precios = sorted(p for it in items_filtrados if (p := it.get("price")) and p > 0)
    if not precios:
        return None
    i = 0
    while i < len(precios) - 1:
        # ¿el actual está descolgado respecto al siguiente? → sospechoso, saltar
        if precios[i] < precios[i + 1] * (1 - gap):
            i += 1
        else:
            break
    return int(precios[i])


# ─────────────────────────────────────────
# Búsqueda con Playwright
# ─────────────────────────────────────────

def _buscar_con_playwright(marca: str, modelo: str, año: int, km: int) -> dict:
    import tempfile, shutil
    from playwright.sync_api import sync_playwright

    urls = _construir_urls(marca, modelo, año)
    resultado_vacio = {"items_filtrados": [], "todos_items": [], "precios_mercado_regex": [], "n_super_precio": 0, "url": ""}

    # coches.net bloquea el modo headless (fingerprinting avanzado).
    # Usamos headless=False con ventana fuera de pantalla (-10000,-10000).
    # Perfil temporal limpio por sesión para evitar acumulación de señales de bot.
    tmp_profile = tempfile.mkdtemp(prefix="cn_")
    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=tmp_profile,
                headless=False,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--window-position=-10000,-10000",
                    "--window-size=1280,800",
                ],
                ignore_default_args=["--enable-automation"],
            )
            page = context.new_page()
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )

            resultado = resultado_vacio.copy()

            for url in urls:
                try:
                    logger.debug(f"   coches.net: probando {url}")
                    page.goto(url, timeout=20000, wait_until="domcontentloaded")
                    time.sleep(1.5)

                    script_data = page.evaluate(_JS_EXTRACTOR)
                    if not script_data.get("found"):
                        logger.debug(f"   coches.net: sin __INITIAL_PROPS__ en {url}")
                        continue

                    todos_items = _parsear_items(script_data["text"])

                    # Fallback regex si el parseo JSON falla
                    precios_regex = [
                        int(m)
                        for m in re.findall(r'\\"priceAverageIndicator\\":(\d+)', script_data["text"])
                        if int(m) > 500
                    ]

                    if not todos_items:
                        logger.debug(f"   coches.net: 0 items JSON, {len(precios_regex)} precios regex en {url}")
                        if precios_regex and not resultado.get("url"):
                            resultado = {"items_filtrados": [], "todos_items": [], "precios_mercado_regex": precios_regex, "n_super_precio": 0, "url": url}
                        continue

                    filtrados = _filtrar_items(todos_items, año, km)
                    n_super = sum(1 for it in todos_items if it.get("priceRankIndicator") == 1)

                    logger.debug(
                        f"   coches.net: {len(todos_items)} items | "
                        f"{n_super} super precio | {len(filtrados)} tras filtros | url={url}"
                    )
                    logger.info(
                        f"   coches.net: {len(todos_items)} anuncios, {n_super} super precio, {len(filtrados)} válidos tras filtros"
                    )

                    candidato = {"items_filtrados": filtrados, "todos_items": todos_items, "precios_mercado_regex": precios_regex, "n_super_precio": n_super, "url": url}
                    if len(filtrados) >= MIN_SUPER_PRECIO:
                        resultado = candidato
                        break
                    elif not resultado.get("url") or len(filtrados) > len(resultado.get("items_filtrados", [])):
                        resultado = candidato

                except Exception as e:
                    logger.debug(f"   coches.net error en {url}: {e}")
                    continue

            context.close()
    finally:
        shutil.rmtree(tmp_profile, ignore_errors=True)

    return resultado


# ─────────────────────────────────────────
# Función pública
# ─────────────────────────────────────────

def precio_mercado_cochesnet(marca: str, modelo: str, año: int, km: int) -> dict:
    """
    Precio de referencia según coches.net. Prioridad:
    A. Más barato realista con "Super precio"  (rank 1)
    B. Más barato realista con "Buen precio"   (rank 2)
    C. Más barato realista sin filtro de rank  (todos los anuncios limpios)

    "Más barato realista" = el anuncio más barato descartando precios ridículos
    (los que están >30% por debajo del siguiente). Además se excluyen coches con
    averías y se filtra por año/km similar (lógica de precio techo).
    """
    resultado_vacio = {"precio_medio": None, "n_anuncios": 0, "fuente": "cochesnet", "url": ""}

    try:
        datos = _buscar_con_playwright(marca, modelo, año, km)
        todos_items = datos.get("todos_items", [])
        url = datos.get("url", "")

        if not todos_items:
            logger.info(f"   coches.net: sin anuncios para {marca} {modelo} {año}")
            return resultado_vacio

        # Filtro base: sin averías + año/km similares (sin restricción de rank)
        base = _filtrar_items(todos_items, año, km)

        def _mas_barato(items, rank=None):
            pool = [it for it in items if rank is None or it.get("priceRankIndicator") == rank]
            if len(pool) < 1:
                return None, 0
            precio = _precio_mas_barato_realista(pool)
            return precio, len(pool)

        # Plan A — Super precio
        precio, n = _mas_barato(base, rank=1)
        if precio and n >= 1:
            logger.info(f"   coches.net ✅ {marca} {modelo} {año} [A-Super precio]: {n} anuncios | más barato realista={precio:,}€")
            return {"precio_medio": precio, "n_anuncios": n, "fuente": "cochesnet", "url": url}

        # Plan B — Buen precio
        precio, n = _mas_barato(base, rank=2)
        if precio and n >= 1:
            logger.info(f"   coches.net ✅ {marca} {modelo} {año} [B-Buen precio]: {n} anuncios | más barato realista={precio:,}€")
            return {"precio_medio": precio, "n_anuncios": n, "fuente": "cochesnet", "url": url}

        # Plan C — Todos los anuncios limpios
        precio, n = _mas_barato(base, rank=None)
        if precio and n >= 1:
            logger.info(f"   coches.net ✅ {marca} {modelo} {año} [C-Todos]: {n} anuncios | más barato realista={precio:,}€")
            return {"precio_medio": precio, "n_anuncios": n, "fuente": "cochesnet", "url": url}

        logger.info(f"   coches.net: sin anuncios válidos tras filtros para {marca} {modelo} {año}")
        return resultado_vacio

    except Exception as e:
        logger.warning(f"⚠️  coches.net: error inesperado para {marca} {modelo}: {e}")
        return resultado_vacio


# ─────────────────────────────────────────
# Enriquecer lista de coches
# ─────────────────────────────────────────

def enriquecer_con_precios_cochesnet(coches: list) -> list:
    """
    Añade precio_mercado_cochesnet a cada coche usando coches.net.
    Agrupa por marca/modelo/bloque-de-años para reutilizar caché entre años similares.
    """
    cache = _cargar_cache()

    for coche in coches:
        marca = coche.get("marca", "")
        modelo = coche.get("modelo", "")
        año = coche.get("año") or 0
        km = coche.get("km") or 0

        # Agrupar en bloques de 2 años para reutilizar cache entre años similares
        año_bloque = (año // 2) * 2
        clave = f"{_marca_slug(marca)}_{_modelo_slug(modelo)}_{año_bloque}"

        if clave in cache:
            mercado = cache[clave]
            logger.debug(f"   coches.net [cache]: {marca} {modelo} {año} → {mercado.get('precio_medio')}€")
        else:
            time.sleep(random.uniform(2, 4))
            mercado = precio_mercado_cochesnet(marca, modelo, año, km)
            cache[clave] = {**mercado, "fecha": str(date.today())}
            _guardar_cache(cache)

        coche["precio_mercado_cochesnet"] = mercado.get("precio_medio")
        coche["cochesnet_url"] = mercado.get("url", "")

    return coches
