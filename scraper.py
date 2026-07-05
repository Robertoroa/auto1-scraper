"""
SCRAPER AUTO1 — Solo lectura, sin clicks, máximo 6 coches.
Siempre ejecutar con DRY_RUN=True la primera vez.
"""

import json
import time
import logging
import requests
from datetime import datetime
from pathlib import Path

from safety import safe_request, MAX_CARS, DRY_RUN, SecurityError
from auth import cargar_cookies, cookies_a_header

# ─────────────────────────────────────────
# Configuración de logging
# ─────────────────────────────────────────
logs_dir = Path(__file__).parent / "logs"
logs_dir.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(logs_dir / f"scraper_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("auto1_scraper")

# ─────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────
BASE_URL = "https://www.auto1.com"
CONFIG_FILE = Path(__file__).parent / "config.json"


def _resolver_config_file() -> Path:
    """Permite pasar --config <ruta> por CLI para scraping puntual sin tocar config.json."""
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", default=None)
    args, _ = parser.parse_known_args()
    return Path(args.config) if args.config else CONFIG_FILE


def cargar_config() -> dict:
    with open(_resolver_config_file(), "r") as f:
        return json.load(f)


def construir_body_busqueda(filtros: dict) -> dict:
    """
    Construye el body del POST de búsqueda.
    Formato verificado empíricamente: el formato complejo con sort/selectedChannel
    devuelve 0 resultados. El formato simplificado devuelve resultados correctos.
    """
    page_size = min(filtros.get("pageSize", 6), MAX_CARS)

    filters = {
        "channel":  filtros.get("channel", "24h"),
        "countries": filtros.get("countries", ["ES"]),
        "sorting":  filtros.get("sort", "relevanceSorting"),
        "page":     0,
        "pageSize": page_size,
        "offset":   0,
        "priceRange":              {},
        "mileageRange":            {},
        "firstRegistrationRange":  {},
    }

    if filtros.get("mileageTo") is not None:
        filters["mileageRange"]["to"] = filtros["mileageTo"]
    if filtros.get("mileageFrom"):
        filters["mileageRange"]["from"] = filtros["mileageFrom"]
    if filtros.get("priceMin"):
        filters["priceRange"]["from"] = filtros["priceMin"]
    if filtros.get("priceMax"):
        filters["priceRange"]["to"] = filtros["priceMax"]
    if filtros.get("regFrom"):
        filters["firstRegistrationRange"]["from"] = filtros["regFrom"]
    if filtros.get("regTo"):
        filters["firstRegistrationRange"]["to"] = filtros["regTo"]

    if filtros.get("bodyTypes"):
        filters["bodyTypes"] = filtros["bodyTypes"]

    if filtros.get("makes"):
        filters["makes"] = filtros["makes"]
    elif filtros.get("manufacturers"):
        filters["makes"] = [{"make": m} for m in filtros["manufacturers"]]

    return {
        "textSearchId": None,
        "filters": filters,
        "supportedFeatures": ["dealer_a"],
        "useAggregations": True,
    }


def obtener_dealer_uuid(session, headers) -> str:
    """
    Obtiene el UUID del dealer desde el perfil.
    Es necesario para construir los endpoints.
    """
    # El UUID está en la URL de la sesión activa
    # Lo extraemos de la respuesta de perfil
    url = f"{BASE_URL}/es/app/merchant/cars"
    response = session.get(url, headers=headers, allow_redirects=True)

    # Buscamos el UUID en la URL final o en el contenido
    import re
    uuid_pattern = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
    uuids = re.findall(uuid_pattern, response.text)

    if uuids:
        logger.info(f"UUID del dealer detectado: {uuids[0]}")
        return uuids[0]

    # Fallback: usar el UUID conocido de la sesión anterior
    logger.warning("UUID no detectado automáticamente, usando el de la sesión anterior")
    return "a37c89c0-2010-4bf1-9edd-5f83ac84df7c"


def obtener_bearer_y_uuid(cookies: dict) -> tuple:
    """
    Lanza Playwright headless, carga la página de dealer con las cookies,
    intercepta la primera llamada a car-search/cars/search y extrae
    el Bearer token y el UUID del dealer.
    """
    from playwright.sync_api import sync_playwright
    import re

    bearer = None
    uuid = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()

        # Inyectar cookies guardadas
        for name, value in cookies.items():
            context.add_cookies([{"name": name, "value": value, "domain": ".auto1.com", "path": "/"}])

        page = context.new_page()

        def on_request(request):
            nonlocal bearer, uuid
            if "car-search/cars/search" in request.url and not bearer:
                auth = request.headers.get("authorization", "")
                if auth.startswith("Bearer "):
                    bearer = auth.replace("Bearer ", "")
                # Extraer UUID de la URL
                m = re.search(r'search/([0-9a-f-]{36})', request.url)
                if m:
                    uuid = m.group(1)
                try:
                    logger.debug(f"   📡 API URL capturada: {request.url}")
                except Exception:
                    pass

        page.on("request", on_request)

        try:
            page.goto("https://www.auto1.com/es/app/merchant/cars", wait_until="domcontentloaded", timeout=20000)
            # Esperar hasta 10s a que llegue el token
            for _ in range(20):
                if bearer:
                    break
                page.wait_for_timeout(500)
        except Exception as e:
            logger.warning(f"⚠️  Timeout cargando página: {e}")
        finally:
            browser.close()

    return bearer, uuid


def buscar_coches_via_playwright(cookies, filtros) -> list:
    """
    Usa Playwright headless para buscar coches con filtros aplicados.

    Estrategia:
    - Si hay makes/modelos específicos → navega a la URL de Auto1 con el param
      'manufacturers=Make|Model;Make|Model...' (mismo formato que usa el web),
      captura las respuestas del endpoint saved-search y pagina hasta obtener todos.
      Auto1 aplica el filtro server-side → resultados idénticos al web.
    - Si no hay makes (solo bodyTypes) → POST a cars/search (más rápido para
      stock general sin filtro de marca).
    """
    from playwright.sync_api import sync_playwright
    import re as _re
    import json as _json
    from urllib.parse import urlencode

    page_size = min(filtros.get("pageSize", 6), MAX_CARS)
    canal = filtros.get("channel", "24h")
    makes_filtro = filtros.get("makes", [])

    # ─── Nombres propios de modelos para construir el param manufacturers ───
    # Clave: nombre genérico (minúsculas) que el usuario selecciona en la UI
    # Valor: lista de nombres exactos que usa Auto1 internamente
    _MODEL_PROPER_NAMES = {
        "transporter":   ["Transporter", "T4 Transporter", "T4 Caravelle", "T4 Kombi", "T4 Multivan",
                          "T5 Transporter", "T5 Caravelle", "T5 Kombi", "T5 Multivan", "T5 Shuttle",
                          "T5 California", "T6 Transporter", "T6 Caravelle", "T6 Kombi", "T6 Multivan",
                          "T7 Kombi", "T7 Multivan"],
        "multivan":      ["Multivan", "T5 Multivan", "T6 Multivan", "T7 Multivan"],
        "crafter":       ["Crafter", "TGE"],
        "proace":        ["ProAce", "ProAce City", "ProAce City Verso", "ProAce Max", "ProAce Verso"],
        "jumpy":         ["Jumpy", "SpaceTourer", "Dispatch"],
        "berlingo":      ["Berlingo", "Berlingo First", "Berlingo Van"],
        "expert":        ["Expert", "Expert Tepee", "Traveller"],
        "partner":       ["Partner", "Rifter"],
        "vito":          ["Vito", "Vito Tourer"],
        "sprinter":      ["Sprinter", "Sprinter Classic"],
        "trafic":        ["Trafic", "Primastar"],
        "master":        ["Master"],
        "vivaro":        ["Vivaro", "Vivaro-E"],
        "movano":        ["Movano"],
        "ducato":        ["Ducato"],
        "jumper":        ["Jumper"],
        "boxer":         ["Boxer"],
        "transit":       ["Transit", "Transit Custom", "Transit Connect", "Transit Courier",
                          "Tourneo", "Tourneo Custom", "Tourneo Connect", "Tourneo Courier",
                          "Grand Tourneo Connect"],
        "nv200":         ["NV200", "NV200 Evalia", "Townstar"],
        "nv300":         ["NV300", "NV250"],
        "nv400":         ["NV400"],
        # Sinónimos para Python-side match (campo mainType de la API)
        "t5 caravelle":  ["T5 Caravelle"],
        "t5 kombi":      ["T5 Kombi"],
        "proace city verso": ["ProAce City Verso"],
        "vito tourer":   ["Vito Tourer"],
    }

    # ─── Sinónimos en minúsculas para matching Python-side (fallback) ────────
    _MODEL_SYNONYMS = {
        "transporter":   ["transporter","t4 transporter","t4 caravelle","t4 kombi","t4 multivan",
                          "t5 transporter","t5 caravelle","t5 kombi","t5 multivan","t5 shuttle",
                          "t5 california","t6 transporter","t6 caravelle","t6 kombi","t6 multivan",
                          "t7 kombi","t7 multivan"],
        "multivan":      ["multivan","t5 multivan","t6 multivan","t7 multivan"],
        "crafter":       ["crafter","tge"],
        "proace":        ["proace","proace city","proace city verso","proace verso","proace max"],
        "jumpy":         ["jumpy","spacetourer","dispatch"],
        "berlingo":      ["berlingo","berlingo first","berlingo van"],
        "expert":        ["expert","expert tepee","traveller"],
        "partner":       ["partner","rifter"],
        "vito":          ["vito","vito tourer"],
        "sprinter":      ["sprinter","sprinter classic"],
        "trafic":        ["trafic","primastar"],
        "master":        ["master","movano"],
        "vivaro":        ["vivaro","vivaro-e"],
        "movano":        ["movano","master"],
        "ducato":        ["ducato","jumper","boxer"],
        "transit":       ["transit","transit custom","transit connect","transit courier",
                          "tourneo","tourneo custom","tourneo connect","tourneo courier",
                          "grand tourneo connect"],
        "nv200":         ["nv200","nv200 evalia","townstar"],
        "nv300":         ["nv300","nv250"],
        "nv400":         ["nv400"],
    }

    def _expandir_modelo(md: str) -> list:
        return _MODEL_SYNONYMS.get(md, [md])

    def _norm(s: str) -> str:
        return s.lower().replace("ë","e").replace("é","e").replace("è","e").replace("ö","o").replace("ü","u").replace("-"," ").strip()

    def _match_make(coche):
        if not makes_filtro:
            return True
        make_name = _norm(coche.get("manufacturerName") or "")
        model_name = _norm(coche.get("mainType") or "")
        for entry in makes_filtro:
            mk_norm = _norm(entry.get("make", ""))
            mds_raw = [m.lower() for m in entry.get("models", [])]
            mds_expanded = []
            for md in mds_raw:
                mds_expanded.extend(_expandir_modelo(md))
            if mk_norm and not (mk_norm in make_name or make_name in mk_norm):
                continue
            if not mds_raw:
                return True
            if any(md in model_name or model_name in md for md in mds_expanded if md):
                return True
        return False

    def _build_manufacturers_param(makes_filtro: list) -> str:
        """
        Convierte la lista makes_filtro al formato de Auto1:
        'Toyota|ProAce;Toyota|ProAce City Verso;Renault|Trafic;...'
        Expande cada modelo genérico a todos sus nombres propios en Auto1.
        """
        parts = []
        seen = set()
        for entry in makes_filtro:
            make = entry.get("make", "").strip()
            models_raw = [m.lower().strip() for m in entry.get("models", [])]
            if not models_raw:
                key = make
                if key not in seen:
                    parts.append(make)
                    seen.add(key)
                continue
            for md in models_raw:
                proper_names = _MODEL_PROPER_NAMES.get(md, [md.title()])
                for name in proper_names:
                    key = f"{make}|{name}"
                    if key not in seen:
                        parts.append(key)
                        seen.add(key)
        return ";".join(parts)

    from pathlib import Path as _Path
    import time as _time
    _PROFILE_DIR = _Path(__file__).parent / "browser_profile"
    _PROFILE_DIR.mkdir(exist_ok=True)

    hits_filtrados = []
    total_api = 0

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(_PROFILE_DIR),
            headless=True,
        )
        page = context.new_page()

        # Verificar sesión y loguearse si hace falta
        page.goto("https://www.auto1.com/es/app/merchant/cars", wait_until="domcontentloaded", timeout=30000)
        _time.sleep(2)
        _current_url = page.url
        if not any(x in _current_url for x in ["merchant", "/app/"]):
            logger.info("   Sesión caducada en perfil — autologin dentro del scraper...")
            from auth import AUTO1_SIGNIN_URL, _cargar_credenciales
            _email, _password = _cargar_credenciales()
            page.goto(AUTO1_SIGNIN_URL, wait_until="domcontentloaded", timeout=20000)
            _time.sleep(2)
            try:
                _cb = page.locator("button:has-text('ACEPTAR')")
                if _cb.is_visible(timeout=2000):
                    _cb.click()
                    _time.sleep(0.5)
            except Exception:
                pass
            page.locator("#login-email").first.fill(_email, force=True)
            _time.sleep(0.3)
            page.locator("#login-password").first.fill(_password, force=True)
            _time.sleep(0.3)
            page.locator("button.btn-primary:visible").first.click(force=True)
            for _ in range(30):
                _time.sleep(1)
                if any(x in page.url for x in ["merchant/cars", "/app/"]):
                    logger.info("   Autologin exitoso dentro del scraper")
                    break
        else:
            logger.info("   Sesión activa en perfil persistente")

        if DRY_RUN:
            context.close()
            logger.info("🔵 DRY_RUN: no se procesa la búsqueda")
            return []

        # ── Siempre usar Estrategia B: construir POST body propio + filtro client-side ──
        # Estrategia A (capturar POST del navegador) fue abandonada porque el perfil
        # persistente devuelve respuestas cacheadas con bodyTypes/carFilters vacíos.
        # _match_make() ya filtra por marca Y modelo correctamente en client-side.
        has_specific_models = False  # forzar siempre Estrategia B

        if has_specific_models:
            # ── ESTRATEGIA A: Capturar POST body que genera la propia app ────
            # Navegar a la URL con manufacturers → la app React convierte los
            # URL params a carFilters con IDs numéricos y hace un POST a cars/search.
            # Interceptamos ese POST body y lo reutilizamos para paginar.
            manufacturers_param = _build_manufacturers_param(makes_filtro)
            logger.info(f"   🔎 manufacturers={manufacturers_param[:100]}{'...' if len(manufacturers_param)>100 else ''}")

            # Capturar URL de búsqueda, Bearer y POST body de la app
            search_url_captured = {}
            bearer_captured = {}
            post_body_captured = {}
            search_response_captured = {}

            def on_request_a(request):
                if "car-search/cars/search/" in request.url:
                    if not search_url_captured:
                        search_url_captured["url"] = request.url
                    auth = request.headers.get("authorization", "")
                    if auth and not bearer_captured:
                        bearer_captured["token"] = auth
                    if not post_body_captured:
                        try:
                            body = request.post_data_json
                            if body:
                                post_body_captured["body"] = body
                        except Exception:
                            pass
                elif "auto1.com/v1/" in request.url and not bearer_captured:
                    auth = request.headers.get("authorization", "")
                    if auth:
                        bearer_captured["token"] = auth

            def on_response_a(response):
                if "car-search/cars/search/" in response.url and not search_response_captured:
                    try:
                        data = response.json()
                        total = data.get("totalHits", data.get("totalCount", 0))
                        hits = data.get("hits", data.get("cars", []))
                        search_response_captured["total"] = total
                        search_response_captured["hits"] = hits
                    except Exception:
                        pass

            page.on("request", on_request_a)
            page.on("response", on_response_a)

            # Navegar a la URL con todos los filtros
            url_params = {
                "channel": canal,
                "dir": filtros.get("dir", "asc"),
                "sort": filtros.get("sort", "relevanceSorting"),
            }
            if filtros.get("priceMax"):
                url_params["priceMax"] = filtros["priceMax"]
            if filtros.get("priceMin"):
                url_params["priceMin"] = filtros["priceMin"]
            if filtros.get("regFrom"):
                url_params["regFrom"] = filtros["regFrom"]
            if manufacturers_param:
                url_params["manufacturers"] = manufacturers_param

            nav_url = "https://www.auto1.com/es/app/merchant/cars?" + urlencode(url_params)
            logger.info(f"🌐 Cargando página con filtros: {nav_url[:120]}...")
            page.goto(nav_url, wait_until="domcontentloaded", timeout=30000)

            # Esperar a que la app haga el POST
            for _ in range(30):
                if post_body_captured.get("body"):
                    break
                page.wait_for_timeout(500)

            if not post_body_captured.get("body"):
                logger.warning("⚠️  No se capturó el POST body de la app, usando body generado")
                post_body_captured["body"] = construir_body_busqueda(filtros)

            search_url = search_url_captured.get("url", "")
            if not search_url:
                import re as _re2
                m = _re2.search(r'/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', nav_url)
                if not m:
                    # fallback: extraer del bearer o usar el conocido
                    search_url = f"https://www.auto1.com/v1/car-search/cars/search/a37c89c0-2010-4bf1-9edd-5f83ac84df7c"
            logger.info(f"   🔗 URL búsqueda: {search_url}")

            # Recoger hits de la primera respuesta interceptada
            ids_vistos = set()
            hits_filtrados = []
            total_api = search_response_captured.get("total", 0)
            for h in search_response_captured.get("hits", []):
                hid = str(h.get("id") or h.get("auctionIdentifier") or "")
                if hid not in ids_vistos:
                    ids_vistos.add(hid)
                    hits_filtrados.append(h)

            logger.info(f"   📥 Total en API: {total_api} | Página 0 (interceptada): {len(hits_filtrados)}")

            # Paginar usando el POST body capturado (con carFilters correctos)
            base_body = post_body_captured["body"]
            bearer = bearer_captured.get("token", "")
            PAGE_SIZE_API = 100
            pagina = 1
            offset = len(hits_filtrados)

            while total_api > 0 and len(hits_filtrados) < total_api:
                # Modificar body para la siguiente página
                import copy as _copy
                pag_body = _copy.deepcopy(base_body)
                if "filters" in pag_body:
                    pag_body["filters"]["page"] = pagina
                    pag_body["filters"]["pageSize"] = PAGE_SIZE_API
                    pag_body["filters"]["offset"] = offset if "offset" in pag_body.get("filters", {}) else offset
                else:
                    pag_body["page"] = pagina
                    pag_body["pageSize"] = PAGE_SIZE_API

                result = page.evaluate("""
                    async ([url, body, bearer]) => {
                        try {
                            const headers = { 'Content-Type': 'application/json', 'Accept': 'application/json' };
                            if (bearer) headers['Authorization'] = bearer;
                            const r = await fetch(url, {
                                method: 'POST',
                                headers: headers,
                                body: JSON.stringify(body),
                                credentials: 'include'
                            });
                            const data = await r.json();
                            return { status: r.status, data: data };
                        } catch(e) {
                            return { error: e.toString() };
                        }
                    }
                """, [search_url, pag_body, bearer])

                if not result or result.get("error") or result.get("status") == 401:
                    logger.error(f"❌ Error paginando (página {pagina}): {result}")
                    break

                data = result.get("data", {})
                hits_pagina = data.get("hits", data.get("cars", []))
                if not hits_pagina:
                    break
                nuevos = 0
                for h in hits_pagina:
                    hid = str(h.get("id") or h.get("auctionIdentifier") or "")
                    if hid not in ids_vistos:
                        ids_vistos.add(hid)
                        hits_filtrados.append(h)
                        nuevos += 1
                logger.info(f"   📄 Página {pagina}: {len(hits_pagina)} recibidos, {nuevos} nuevos → acumulado: {len(hits_filtrados)}")
                pagina += 1
                offset += len(hits_pagina)

            logger.info(f"   ✅ Stock descargado: {len(hits_filtrados)} coches (API reporta {total_api})")

        else:
            # ── ESTRATEGIA B: POST cars/search (sin filtro de modelos) ───────
            # Capturar la URL de búsqueda y Bearer de la primera request
            first_search = {}
            dealer_uuid_found = {}
            bearer_captured = {}

            def on_request(request):
                if "car-search/cars/search" in request.url and not first_search:
                    first_search["url"] = request.url
                    auth_header = request.headers.get("authorization", "")
                    if auth_header and not bearer_captured:
                        bearer_captured["token"] = auth_header
                m = _re.search(r'car-search/[^/]+/([0-9a-f-]{36})', request.url)
                if m and not dealer_uuid_found:
                    dealer_uuid_found["uuid"] = m.group(1)
                if "auto1.com/v1/" in request.url and not bearer_captured:
                    auth_header = request.headers.get("authorization", "")
                    if auth_header:
                        bearer_captured["token"] = auth_header

            page.on("request", on_request)

            nav_url = f"https://www.auto1.com/es/app/merchant/cars?channel={canal}&dir={filtros.get('dir','asc')}&sort={filtros.get('sort','relevanceSorting')}"
            logger.info(f"🌐 Cargando página con filtros: {nav_url}")
            page.goto(nav_url, wait_until="domcontentloaded", timeout=30000)

            for _ in range(20):
                if first_search.get("url"):
                    break
                page.wait_for_timeout(500)

            search_url = first_search.get("url", "")
            if not search_url and dealer_uuid_found.get("uuid"):
                uuid = dealer_uuid_found["uuid"]
                search_url = f"https://www.auto1.com/v1/car-search/cars/search/{uuid}"
            if not search_url:
                try:
                    bearer, uuid = obtener_bearer_y_uuid(cookies)
                    if uuid:
                        search_url = f"https://www.auto1.com/v1/car-search/cars/search/{uuid}"
                except Exception as e:
                    logger.error(f"❌ No se pudo obtener URL de búsqueda: {e}")

            logger.info(f"   🔗 URL búsqueda: {search_url or 'NO CAPTURADA'}")
            body = construir_body_busqueda(filtros)
            body_types = body["filters"].get("bodyTypes", [])
            makes_log = [m.get('make') for m in makes_filtro] or ["todas"]
            logger.info(f"   🔎 Filtros API: bodyTypes={body_types} | marcas={makes_log} | objetivo={page_size}")

            # Auto1 limita el tamaño de página de búsqueda a 50 (aunque pidas más)
            # y pagina por el campo 'page' (ignora 'offset'). Por eso hay que
            # incrementar 'page' en cada vuelta; si se deja fijo en 0 la API
            # devuelve siempre los mismos 50 coches.
            api_batch = 50
            hits_filtrados = []
            pagina = 0
            offset_actual = 0
            total_api = None
            ids_vistos = set()

            while True:
                body["filters"]["pageSize"] = api_batch
                body["filters"]["page"] = pagina
                body["filters"]["offset"] = 0

                bearer = bearer_captured.get("token", "")
                result = page.evaluate("""
                    async ([url, body, bearer]) => {
                        try {
                            const headers = { 'Content-Type': 'application/json', 'Accept': 'application/json' };
                            if (bearer) headers['Authorization'] = bearer;
                            const r = await fetch(url, {
                                method: 'POST',
                                headers: headers,
                                body: JSON.stringify(body),
                                credentials: 'include'
                            });
                            const data = await r.json();
                            return { status: r.status, data: data };
                        } catch(e) {
                            return { error: e.toString() };
                        }
                    }
                """, [search_url, body, bearer])

                if not result or result.get("error"):
                    logger.error(f"❌ Error en búsqueda (página {pagina}): {result}")
                    break

                status = result.get("status", "?")
                data = result.get("data", {})

                if status == 401:
                    logger.error("❌ 401 — sesión expirada")
                    break

                hits_pagina = data.get("hits", data.get("cars", []))
                if total_api is None:
                    total_api = data.get("totalHits", data.get("totalCount", 0))
                    logger.info(f"   📥 Total en API: {total_api} | Paginando en bloques de {api_batch}")

                if not hits_pagina:
                    logger.info(f"   📄 Página {pagina}: sin más resultados")
                    break

                nuevos = 0
                for h in hits_pagina:
                    hid = str(h.get("id") or h.get("auctionIdentifier") or "")
                    if hid in ids_vistos:
                        continue
                    ids_vistos.add(hid)
                    make_ok = _match_make(h)
                    if make_ok:
                        hits_filtrados.append(h)
                        nuevos += 1

                logger.info(f"   📄 Página {pagina}: {len(hits_pagina)} recibidos, {nuevos} coinciden → acumulado: {len(hits_filtrados)}")

                offset_actual += len(hits_pagina)
                pagina += 1

                if total_api is not None and offset_actual >= total_api:
                    logger.info(f"   ✅ Stock completo descargado ({total_api} coches en API)")
                    break

        context.close()

    # Ordenar por precio ascendente y devolver los page_size más baratos
    def _precio_coche(c):
        buy_now = c.get("buyNowPrice") or 0
        garantizado = c.get("expectedPriceDisplay") or c.get("expectedPriceTarget") or 0
        precio_raw = buy_now or garantizado or c.get("mpPrice") or c.get("searchPrice") or 0
        return round(precio_raw / 100) if precio_raw > 10000 else precio_raw

    hits_filtrados.sort(key=_precio_coche)
    logger.info(f"✅ Total API: {total_api} | Resultados: {len(hits_filtrados)} | Ordenados por precio ↑ | Procesando: {min(len(hits_filtrados), page_size)}")
    return hits_filtrados[:page_size]


def buscar_coches(session, headers, dealer_uuid, filtros) -> list:
    """Delega a Playwright que aplica los filtros desde dentro del browser."""
    cookies_dict = {c.name: c.value for c in session.cookies}
    return buscar_coches_via_playwright(cookies_dict, filtros)


def obtener_detalle_coche(session, headers, coche_id: str) -> dict:
    """
    Obtiene la ficha completa de un coche para verificar daños en motor.
    Solo lectura — GET puro.
    """
    url = f"{BASE_URL}/v1/car-search/cars/{coche_id}"

    logger.info(f"📋 Obteniendo detalle del coche {coche_id}...")
    time.sleep(3)  # Delay de seguridad entre requests

    response = safe_request(session, "GET", url, headers=headers)

    if DRY_RUN:
        return {}

    if response.status_code == 404:
        logger.warning(f"⚠️  FICHA NO DISPONIBLE para {coche_id} — Requiere revisión humana")
        return {"error": "ficha_no_disponible", "id": coche_id}

    response.raise_for_status()
    return response.json()


def tiene_danos_motor(detalle: dict) -> bool:
    """
    Analiza la ficha del coche para detectar daños en motor.
    Soporta la estructura de /v1/car-details-view/{ref}/{uuid} (formato nuevo)
    y /v1/car-search/cars/{id} (formato antiguo, ya no usado).
    Devuelve True si HAY daños en motor, False si no, None si no hay datos.
    """
    if not detalle or detalle.get("error"):
        return None  # No sabemos — requiere revisión humana

    motor_keywords = ["motor", "engine", "getriebe", "transmission", "drivetrain", "antrieb"]

    # ── Formato nuevo: car-details-view ─────────────────────────────────────
    meta = detalle.get("meta", {})
    if meta:
        # 1. Daños estructurados: meta.damages[].subSectionValue
        for dmg in meta.get("damages", []) or []:
            seccion = str(dmg.get("subSectionValue", "")).lower()
            if any(kw in seccion for kw in motor_keywords):
                return True
            for part in dmg.get("parts", []) or []:
                part_key = str(part.get("partKey", "")).lower()
                desc_key = str(part.get("descriptionKey", "")).lower()
                if any(kw in part_key or kw in desc_key for kw in motor_keywords):
                    return True

        # 2. OBD (diagnóstico a bordo): si hay errores → posibles problemas
        obd = meta.get("onBoardDiagnostics")
        if obd and isinstance(obd, dict):
            if obd.get("hasFaults") or obd.get("engineFaults"):
                return True

        # 3. structuredTestDrive: buscar hallazgos de motor
        std = meta.get("structuredTestDrive", {}) or {}
        for row in std.get("rows", []) or []:
            for cell in row.get("cells", []) or []:
                val = str(cell).lower()
                if any(kw in val for kw in motor_keywords):
                    return True

        return False  # Ficha obtenida y sin daños en motor

    # ── Formato antiguo: car-search/cars/{id} ───────────────────────────────
    for finding in detalle.get("testDriveFindings", []) or []:
        if any(kw in str(finding.get("category", "")).lower() for kw in motor_keywords):
            return True

    for damage in detalle.get("damages", []) or []:
        area = str(damage.get("area", "")).lower()
        tipo = str(damage.get("type", "")).lower()
        if any(kw in area or kw in tipo for kw in motor_keywords):
            return True

    return False


# ── Traducciones para el informe de revisión (popup del panel) ──────────────
_TRAD_COMPONENTE = {
    "speedometer": "Velocímetro", "engine": "Motor", "gears": "Cambio",
    "steering": "Dirección", "suspension": "Suspensión", "brakes": "Frenos",
    "clutch": "Embrague", "ac": "Aire acondicionado", "lights": "Luces",
    "navigation system": "Sistema de navegación", "other noise level": "Nivel de ruido",
    "electronics": "Electrónica", "exhaust": "Escape", "battery": "Batería",
    "handbrake": "Freno de mano", "parking brake": "Freno de estacionamiento",
}
_TRAD_DETALLE = {
    "standing": "Parado", "at start": "Al arrancar", "while driving": "En marcha",
    "rough running noise": "Funcionamiento irregular / ruido",
    "unusual noises": "Ruidos anómalos", "no performance": "Falta de potencia",
    "public street lower speed": "Vía pública a baja velocidad",
    "public street higher speed": "Vía pública a alta velocidad",
}
_TRAD_ZONA = {
    "damage body back": "Carrocería · trasera", "damage body front": "Carrocería · delantera",
    "damage body right": "Carrocería · lateral derecho", "damage body left": "Carrocería · lateral izquierdo",
    "damage body top": "Carrocería · techo", "damage interior": "Interior",
    "damage wheels": "Ruedas / llantas", "damage engine": "Motor",
    "damage underbody": "Bajos", "damage glass": "Cristales",
}
_TRAD_PARTE = {
    "tailgate": "Portón trasero", "bonnet": "Capó", "roof": "Techo",
    "front right door": "Puerta delantera derecha", "front left door": "Puerta delantera izquierda",
    "rear right door": "Puerta trasera derecha", "rear left door": "Puerta trasera izquierda",
    "front right fender": "Aleta delantera derecha", "front left fender": "Aleta delantera izquierda",
    "rear right fender": "Aleta trasera derecha", "rear left fender": "Aleta trasera izquierda",
    "bumper front": "Parachoques delantero", "bumper rear": "Parachoques trasero",
    "front bumper": "Parachoques delantero", "rear bumper": "Parachoques trasero",
    "hood": "Capó", "windscreen": "Parabrisas", "rear window": "Luna trasera",
    "left mirror": "Retrovisor izquierdo", "right mirror": "Retrovisor derecho",
    "sill": "Faldón lateral", "wheel": "Rueda / llanta",
}
_TRAD_TIPO = {
    "scratch": "Arañazo", "dent": "Abolladura", "rust": "Óxido", "crack": "Grieta",
    "broken": "Roto", "missing": "Falta", "paint damage": "Daño en pintura",
    "chip": "Impacto de piedra", "stone chip": "Impacto de piedra", "scuff": "Rozadura",
    "worn": "Desgaste", "repaint": "Repintado", "body gap": "Desajuste de carrocería",
    "corrosion": "Corrosión", "hail": "Granizo", "bent": "Deformado",
}


def _humanizar(clave: str, tabla: dict) -> str:
    """Traduce una clave i18n; si no está en la tabla, la formatea legible."""
    if not clave:
        return ""
    limpio = (str(clave)
              .replace("global.damages.sub_sections.", "").replace("global.damages.", "")
              .replace("STD.", "").replace(".ok.yes", "").replace(".ok", "")
              .replace(".", " ").replace("-", " ").replace("_", " ").strip().lower())
    if limpio in tabla:
        return tabla[limpio]
    return limpio.capitalize()


def construir_revision(detalle: dict) -> dict:
    """
    Construye un informe de revisión legible (en español) a partir de la ficha
    car-details-view: prueba dinámica, daños de carrocería y accidentes.
    Marca como 'avería' cualquier componente con defecto funcional.
    """
    vacio = {"prueba_dinamica": [], "danos": [], "accidente": None,
             "resumen": {"averias": [], "n_danos": 0, "accidente": False}}
    if not detalle:
        return vacio
    meta = detalle.get("meta", {}) or {}

    # ── Prueba dinámica (structuredTestDrive) ──────────────────────────────
    prueba = []
    averias = []
    std = meta.get("structuredTestDrive") or {}
    for item in (std.get("form") or []):
        comp = _humanizar(item.get("name", ""), _TRAD_COMPONENTE)
        defecto = bool(item.get("defected"))
        detalles = []
        for g in (item.get("groups") or []):
            for tk in (g.get("translationKeys") or []):
                detalles.append(_humanizar(tk, _TRAD_DETALLE))
        prueba.append({"componente": comp, "ok": not defecto, "detalles": detalles})
        if defecto:
            averias.append(comp)

    # ── Daños de carrocería (damages) ──────────────────────────────────────
    danos = []
    n_danos = 0
    for sec in (meta.get("damages") or []):
        zona = _humanizar(sec.get("subSectionValue") or sec.get("subSectionValueKey", ""), _TRAD_ZONA)
        partes = []
        for p in (sec.get("parts") or []):
            partes.append({
                "parte": _humanizar(p.get("partKey", ""), _TRAD_PARTE),
                "tipo": _humanizar(p.get("descriptionKey", ""), _TRAD_TIPO),
            })
            n_danos += 1
        if partes:
            danos.append({"zona": zona, "partes": partes})

    # ── Accidentes ─────────────────────────────────────────────────────────
    accidente = None
    tiene_acc = False
    for a in (meta.get("accidents") or []):
        if a.get("hasAccident"):
            tiene_acc = True
            accidente = {"reparado": a.get("repaired"), "coste": a.get("repairCost")}
            break

    return {
        "prueba_dinamica": prueba,
        "danos": danos,
        "accidente": accidente,
        "resumen": {"averias": averias, "n_danos": n_danos, "accidente": tiene_acc},
    }


def tiene_compra_inmediata(detalle: dict) -> bool:
    """
    Para coches de subasta 24h: devuelve True solo si tienen botón 'Cómpralo ahora'.
    Solo se incluyen coches comprables a precio fijo — nunca entramos en puja.
    Campo correcto: meta.auctionRunning.isBuyNowEligible = true
    """
    if not detalle:
        return False
    meta = detalle.get("meta", {}) or {}
    auction = meta.get("auctionRunning", {}) or {}
    return bool(auction.get("isBuyNowEligible"))


def ejecutar_scraping():
    """
    Función principal. Orquesta todo el proceso de scraping.
    """
    logger.info("="*60)
    logger.info("🚀 INICIO DE SCRAPING AUTO1")
    logger.info(f"   Modo: {'🔵 DRY_RUN (simulación)' if DRY_RUN else '🟢 REAL'}")
    logger.info(f"   Límite máximo de coches: {MAX_CARS}")
    logger.info("="*60)

    # 1. Cargar configuración
    config = cargar_config()
    filtros = config["filtros"]
    delay = config["seguridad"].get("delay_entre_requests_segundos", 3)
    logger.info(f"   Filtros cargados: km={filtros.get('mileageFrom')}-{filtros.get('mileageTo')} | año={filtros.get('regFrom')}-{filtros.get('regTo')} | precio={filtros.get('priceMin')}-{filtros.get('priceMax')} | bodyTypes={filtros.get('bodyTypes')}")

    # 2. Autenticación — obtener Bearer token via Playwright
    logger.info("🔐 Obteniendo token de autenticación...")
    cookies = cargar_cookies()
    if not cookies:
        logger.error("❌ No se pudieron obtener las cookies. Abortando.")
        return

    bearer_token, dealer_uuid = obtener_bearer_y_uuid(cookies)
    if not bearer_token:
        logger.error("❌ No se pudo obtener el Bearer token. Abortando.")
        return

    logger.info(f"✅ Bearer token obtenido. UUID: {dealer_uuid}")

    headers = {
        "Authorization": f"Bearer {bearer_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://www.auto1.com",
        "Referer": "https://www.auto1.com/es/app/merchant/cars",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }

    session = requests.Session()
    for name, value in cookies.items():
        session.cookies.set(name, value, domain=".auto1.com")

    todos_resultados = {}

    # Si el config tiene un campo "canales", usarlo; si no, correr ambos (comportamiento por defecto)
    canales_a_procesar = filtros.get("canales") or ["24h", "ip"]

    for canal in canales_a_procesar:
        logger.info(f"\n{'='*60}")
        logger.info(f"🔍 Scraping canal: {'Subasta 24h' if canal == '24h' else 'Compra Ahora'}")
        logger.info(f"{'='*60}")

        filtros_canal = dict(filtros)
        filtros_canal["channel"] = canal

        # Fetchamos un pool grande para tener margen de filtrado.
        # El objetivo es obtener max_coches válidos — muchos se descartan por filtros.
        max_validos = config["seguridad"].get("max_coches", 10)
        pool_size = min(max(max_validos * 3, 50), MAX_CARS)  # mínimo 50, máximo MAX_CARS (100)
        filtros_canal["pageSize"] = pool_size

        coches = buscar_coches(session, headers, dealer_uuid, filtros_canal)

        if not coches:
            logger.info(f"ℹ️  Sin resultados para canal {canal}")
            todos_resultados[canal] = []
            continue

        logger.info(f"   Pool: {len(coches)} coches para filtrar hasta {max_validos} válidos")

        resultados = []
        saltados_canal_ip = 0
        for i, coche in enumerate(coches, 1):
            if len(resultados) >= max_validos:
                logger.info(f"   ✅ Objetivo alcanzado: {max_validos} coches válidos")
                break

            coche_id = str(coche.get("id") or coche.get("auctionIdentifier") or coche.get("stockId") or coche.get("reference"))
            logger.info(f"\n🚗 [{canal}] Procesando coche {i}/{len(coches)} (válidos: {len(resultados)}/{max_validos}): {coche_id}")

            time.sleep(delay)

            reg = coche.get("firstRegistration") or coche.get("firstRegistrationDate")
            if isinstance(reg, int) and reg > 9999:
                from datetime import datetime
                reg = datetime.fromtimestamp(reg / 1000).year

            # Precio según el tipo de transacción:
            # - INSTANTPURCHASE (Compra Ahora) → buyNowPrice
            # - 24h auction → minimumBid (puja de salida), buyNowPrice es opcional y más caro
            auction_type    = coche.get("auctionType", "")
            es_compra_inmediata = (auction_type == "INSTANTPURCHASE")

            buy_now_raw     = coche.get("buyNowPrice") or 0
            min_bid_raw     = coche.get("minimumBid") or coche.get("auctionStartPrice") or 0
            garantizado_raw = coche.get("expectedPriceDisplay") or coche.get("expectedPriceTarget") or 0

            # Precio de referencia para calcular rentabilidad:
            # 1. buyNowPrice        → "Cómpralo ahora" (precio cierto)
            # 2. expectedPriceDisplay → precio garantizado por Auto1
            # Sin ninguno de los dos → precio incierto, no incluir en resultados
            precio_raw = buy_now_raw or garantizado_raw or 0
            if not precio_raw:
                logger.info(f"   ⏭️  Saltado: sin precio cierto (solo puja mínima, no calculable)")
                continue
            # Auto1 devuelve el precio en dos escalas según el anuncio: en euros
            # (p.ej. 15500 = 15.500€) o en céntimos (p.ej. 1645500 = 16.455€).
            # Los valores en euros de estos furgones no pasan de ~30.000 y los de
            # céntimos empiezan en ~200.000, así que el corte en 100.000 separa
            # ambos sin partir por 100 un buy-now real de más de 10.000€.
            precio = round(precio_raw / 100) if precio_raw >= 100_000 else precio_raw

            referencia = coche.get("stockNumber", coche.get("auctionIdentifier", str(coche_id)))

            # Una sola sesión Playwright: ficha del coche + transporte
            try:
                from transporte import obtener_ficha_y_transporte
                logger.info(f"   🔍 Obteniendo ficha y transporte para {referencia}...")
                ficha = obtener_ficha_y_transporte(referencia, coche_id, cookies)
            except Exception as e:
                logger.warning(f"   ⚠️  Error ficha/transporte {referencia}: {e}")
                ficha = {"detalle": None, "transporte_coste": None, "transporte_dias": None, "transporte_opcion": None}

            detalle = ficha.get("detalle")

            # Determinar tipo de compra y precio mínimo aceptable
            if es_compra_inmediata:
                tipo_compra = "Compra Ahora"
            elif auction_type.startswith("24D"):
                tipo_compra = "Subasta 24h"
            else:
                tipo_compra = auction_type or "Subasta"

            if precio == 0:
                logger.info(f"   ⏭️  Saltado: sin precio ({auction_type})")
                continue

            # Canal 24h: solo subastas y compras inmediatas (no ip-channel puras)
            # Canal ip: solo Compra Ahora (INSTANTPURCHASE)
            if canal == "ip" and not es_compra_inmediata:
                logger.info(f"   ⏭️  Saltado en canal ip: no es Compra Ahora ({auction_type})")
                saltados_canal_ip += 1
                continue

            logger.info(f"   💰 {tipo_compra} ({auction_type}) — precio: {precio}€")

            danos = tiene_danos_motor(detalle)
            revision = construir_revision(detalle)
            averias_prueba = revision.get("resumen", {}).get("averias", []) or []
            transporte = {
                "transporte_coste": ficha.get("transporte_coste"),
                "transporte_dias": ficha.get("transporte_dias"),
                "transporte_opcion": ficha.get("transporte_opcion"),
            }

            # Check del panel: excluir coches con fallo en motor o en prueba dinámica.
            # Por defecto activado (True) para conservar el comportamiento anterior.
            excluir_averias = filtros.get("excluir_averias", True)
            if excluir_averias:
                if danos is True:
                    logger.info(f"   ❌ Descartado: tiene daños en motor")
                    continue
                if averias_prueba:
                    logger.info(f"   ❌ Descartado: avería en prueba dinámica ({', '.join(averias_prueba)})")
                    continue
                if danos is None:
                    logger.warning(f"   ⚠️  REVISIÓN HUMANA REQUERIDA: no se pudo verificar ficha")

            resultados.append({
                "id": coche_id,
                "marca": coche.get("manufacturerName", coche.get("make", "")),
                "modelo": coche.get("mainType", coche.get("model", "")),
                "año": reg,
                "km": coche.get("km", coche.get("mileage", 0)),
                "precio_auto1": precio,
                "tipo_compra": tipo_compra,
                "puja_minima": round(min_bid_raw / 100) if min_bid_raw >= 100_000 else min_bid_raw,
                "pais": coche.get("countryCode", ""),
                "referencia": referencia,
                "canal": canal,
                "danos_motor": danos,
                "requiere_revision": danos is None,
                "transporte_coste": transporte.get("transporte_coste"),
                "transporte_dias": transporte.get("transporte_dias"),
                "transporte_opcion": transporte.get("transporte_opcion"),
                "imagenes": ficha.get("imagenes", []),
                "revision": revision,
                "auto1_url": f"https://www.auto1.com/es/app/merchant/car/{referencia}",
            })

        logger.info(f"\n✅ Canal {canal}: {len(resultados)} coches válidos")
        if canal == "ip" and len(resultados) == 0 and saltados_canal_ip > 0:
            logger.warning(
                f"⚠️  AVISO: {saltados_canal_ip} coches encontrados pero todos son subasta 24h, "
                f"no Compra Ahora. Prueba a esrapear con el canal '24h' en vez de 'ip'."
            )

        # Enriquecer con precios de mercado (Wallapop + coches.net) y rentabilidad real
        if resultados:
            from transporte import calcular_coste_total, calcular_rentabilidad

            # ── Wallapop ──
            try:
                from wallapop import enriquecer_con_precios_mercado
                logger.info(f"\n💰 Buscando precios de mercado en Wallapop...")
                resultados = enriquecer_con_precios_mercado(resultados)
                con_wp = [r for r in resultados if r.get("precio_mercado_wallapop") is not None]
                logger.info(f"✅ Wallapop: precios para {len(con_wp)}/{len(resultados)} coches")
            except Exception as e:
                logger.warning(f"⚠️  Error Wallapop: {e}")

            # ── coches.net ──
            try:
                from cochesnet import enriquecer_con_precios_cochesnet
                logger.info(f"\n💰 Buscando precios de mercado en coches.net...")
                resultados = enriquecer_con_precios_cochesnet(resultados)
                con_cn = [r for r in resultados if r.get("precio_mercado_cochesnet") is not None]
                logger.info(f"✅ coches.net: precios para {len(con_cn)}/{len(resultados)} coches")
            except Exception as e:
                logger.warning(f"⚠️  Error coches.net: {e}")

            # ── Rentabilidad doble ──
            for r in resultados:
                coste_total = calcular_coste_total(r.get("precio_auto1", 0), r.get("transporte_coste"))
                r["coste_total"] = coste_total

                rent_wp = calcular_rentabilidad(r.get("precio_mercado_wallapop"), coste_total)
                r["margen_wallapop"]           = rent_wp["margen_real"]
                r["rentabilidad_wallapop_pct"] = rent_wp["rentabilidad_real_pct"]

                rent_cn = calcular_rentabilidad(r.get("precio_mercado_cochesnet"), coste_total)
                r["margen_cochesnet"]           = rent_cn["margen_real"]
                r["rentabilidad_cochesnet_pct"] = rent_cn["rentabilidad_real_pct"]

        todos_resultados[canal] = resultados

        # Exportar a Google Sheets
        try:
            from google_sheets import actualizar_sheets
            actualizar_sheets(resultados, canal)
        except FileNotFoundError:
            logger.info("ℹ️  Google Sheets no configurado (falta google_credentials.json)")
        except ValueError as e:
            logger.info(f"ℹ️  Google Sheets: {e}")
        except Exception as e:
            import traceback
            logger.warning(f"⚠️  Error en Google Sheets: {e}\n{traceback.format_exc()}")

    # Guardar resultados en JSON para la página visual
    try:
        resultados_dir = Path(__file__).parent / "resultados"
        resultados_dir.mkdir(exist_ok=True)
        latest_file = resultados_dir / "resultados_latest.json"
        with open(latest_file, "w", encoding="utf-8") as f:
            json.dump(todos_resultados, f, ensure_ascii=False, indent=2)
        logger.info(f"✅ Resultados guardados en {latest_file}")
    except Exception as e:
        logger.warning(f"⚠️  No se pudo guardar resultados_latest.json: {e}")

    return todos_resultados


if __name__ == "__main__":
    resultados = ejecutar_scraping()
    if resultados:
        total = sum(len(v) for v in resultados.values())
        print(f"\n📊 Scraping completado: {total} coches procesados")
