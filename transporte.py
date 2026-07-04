"""
Módulo para obtener opciones de transporte desde la página de producto de Auto1.
Usa Playwright para visitar la ficha del coche y extraer la opción más barata con sus días.
"""
from __future__ import annotations

import logging
import re
from playwright.sync_api import sync_playwright

logger = logging.getLogger("auto1_scraper")

# Coste fijo de comisión Auto1 por coche
COMISION_AUTO1 = 500


def obtener_ficha_y_transporte(referencia: str, coche_id: str, cookies: dict) -> dict:
    """
    Visita la ficha del coche UNA SOLA VEZ con Playwright e intercepta:
    - El detalle del coche (/v1/car-search/cars/{id}) para detectar daños
    - Las opciones de transporte (/v1/merchant-route/*/delivery-options)

    Devuelve:
    {
        "detalle": {...},           # datos de la ficha (None si no disponible)
        "transporte_coste": 290,
        "transporte_dias": 5,
        "transporte_opcion": "Depósito",
    }
    """
    url_coche = f"https://www.auto1.com/es/app/merchant/car/{referencia}"
    captured_detalle = {}
    captured_transporte = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        for name, value in cookies.items():
            context.add_cookies([{
                "name": name, "value": value,
                "domain": ".auto1.com", "path": "/"
            }])

        page = context.new_page()

        def on_response(response):
            url = response.url
            if response.status != 200:
                return

            # Detalle del coche — endpoint real: /v1/car-details-view/{referencia}/{dealer-uuid}
            if not captured_detalle and f"/v1/car-details-view/{referencia}/" in url:
                try:
                    data = response.json()
                    if isinstance(data, dict):
                        captured_detalle["data"] = data
                        captured_detalle["url"] = url
                        logger.info(f"   📋 Ficha capturada para {referencia}")
                except Exception:
                    pass

            # Opciones de transporte
            if "delivery-options" in url and not captured_transporte:
                try:
                    data = response.json()
                    captured_transporte["raw"] = data
                    captured_transporte["url"] = url
                except Exception:
                    pass

        page.on("response", on_response)

        imagenes_dom = []
        try:
            page.goto(url_coche, wait_until="domcontentloaded", timeout=25000)
            for _ in range(30):
                if captured_transporte and (captured_detalle or True):
                    break
                page.wait_for_timeout(500)

            # Extraer URLs de imágenes del DOM
            imagenes_dom = page.evaluate("""() => {
                const imgs = [];
                // 1. <img> con src de auto1 o CDN de fotos
                document.querySelectorAll('img').forEach(el => {
                    const src = el.src || el.getAttribute('data-src') || '';
                    if (src && (src.includes('auto1') || src.includes('img.') || src.includes('/photo'))
                        && !src.includes('logo') && !src.includes('icon') && src.startsWith('http')) {
                        imgs.push(src);
                    }
                });
                // 2. Buscar en __NEXT_DATA__ o estado inicial embebido
                try {
                    const scripts = Array.from(document.querySelectorAll('script'));
                    for (const s of scripts) {
                        const t = s.textContent || '';
                        const matches = t.match(/https?:\\/\\/[^"'\\s]+\\.(?:jpg|jpeg|webp|png)[^"'\\s]*/g);
                        if (matches) matches.forEach(u => imgs.push(u));
                    }
                } catch(e) {}
                // Deduplicar y filtrar
                return [...new Set(imgs)].filter(u =>
                    !u.includes('logo') && !u.includes('icon') && !u.includes('flag') &&
                    !u.includes('badge') && !u.includes('avatar')
                ).slice(0, 25);
            }""")
            if imagenes_dom:
                logger.info(f"   📷 {len(imagenes_dom)} fotos extraídas del DOM para {referencia}")
        except Exception as e:
            logger.warning(f"⚠️  Error cargando ficha {referencia}: {e}")
        finally:
            browser.close()

    if not captured_detalle:
        logger.warning(f"   ⚠️  Ficha no capturada para {referencia} (coche_id={coche_id})")

    transporte = _parsear_transporte(captured_transporte, referencia)
    return {
        "detalle": captured_detalle.get("data"),
        "imagenes": imagenes_dom,
        **transporte,
    }


def obtener_transporte(referencia: str, cookies: dict) -> dict:
    """
    Visita la página de producto del coche en Auto1 e intercepta la llamada
    a la API de opciones de transporte.

    Devuelve:
    {
        "coste": 290,
        "dias": 5,
        "opcion": "Económico",
        "todas_opciones": [...]
    }
    """
    url_coche = f"https://www.auto1.com/es/app/merchant/car/{referencia}"
    captured = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )

        # Inyectar cookies de sesión
        for name, value in cookies.items():
            context.add_cookies([{
                "name": name,
                "value": value,
                "domain": ".auto1.com",
                "path": "/"
            }])

        page = context.new_page()

        logistics_calls = []

        def on_response(response):
            url = response.url
            # Capturar todas las respuestas del dominio de logística de Auto1
            if "logistics.auto1" in url or "auto1-apps" in url:
                logistics_calls.append(f"{response.status} {url}")
                if response.status == 200:
                    try:
                        data = response.json()
                        if not captured and isinstance(data, (dict, list)):
                            # Buscar estructura con opciones de transporte
                            has_transport = False
                            if isinstance(data, list) and len(data) > 0:
                                has_transport = True
                            elif isinstance(data, dict):
                                for key in ["quotes", "options", "transportOptions", "deliveryOptions", "items", "services"]:
                                    if key in data and data[key]:
                                        has_transport = True
                                        break
                            if has_transport:
                                captured["raw"] = data
                                captured["url"] = url
                                logger.info(f"   ✅ Transporte API: {url}")
                    except Exception:
                        pass
            # También interceptar patrones clásicos de transporte
            elif response.status == 200 and any(k in url for k in ["transport", "shipping", "delivery", "checkout"]):
                try:
                    data = response.json()
                    if not captured and isinstance(data, (dict, list)):
                        captured["raw"] = data
                        captured["url"] = url
                        logger.info(f"   ✅ Transporte (classic): {url}")
                except Exception:
                    pass

        page.on("response", on_response)

        try:
            page.goto(url_coche, wait_until="domcontentloaded", timeout=25000)

            # Esperar hasta 15s a que cargue la sección de transporte
            for _ in range(30):
                if captured:
                    break
                page.wait_for_timeout(500)

            # Log de todas las llamadas al dominio logistics para debug
            if not captured and logistics_calls:
                logger.info(f"   🔍 Logistics calls ({referencia}): {logistics_calls[:15]}")

            # Si no interceptamos API, intentamos leer el DOM directamente
            if not captured:
                captured["dom"] = _extraer_transporte_dom(page)

        except Exception as e:
            logger.warning(f"⚠️  Error cargando página de transporte para {referencia}: {e}")
        finally:
            browser.close()

    return _parsear_transporte(captured, referencia)


def _extraer_imagenes_deep(obj, depth=0, found=None) -> list:
    """Busca recursivamente URLs de imagen en cualquier estructura JSON."""
    if found is None:
        found = []
    if depth > 6 or len(found) >= 20:
        return found
    if isinstance(obj, str):
        if obj.startswith("http") and any(x in obj for x in [".jpg", ".webp", ".jpeg", ".png", "img.", "/photo", "/image"]):
            found.append(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _extraer_imagenes_deep(v, depth + 1, found)
    elif isinstance(obj, list):
        for item in obj:
            _extraer_imagenes_deep(item, depth + 1, found)
    return found


def _extraer_imagenes(detalle: dict) -> list:
    """
    Extrae URLs de imágenes del JSON de detalle del coche (car-details-view).
    Prueba varios campos comunes de la API de Auto1.
    """
    urls = []
    # Campos donde Auto1 suele poner las fotos
    for key in ("images", "photos", "pictures", "media", "gallery"):
        val = detalle.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str) and item.startswith("http"):
                    urls.append(item)
                elif isinstance(item, dict):
                    for sub in ("url", "src", "href", "original", "large", "medium"):
                        u = item.get(sub)
                        if u and isinstance(u, str) and u.startswith("http"):
                            urls.append(u)
                            break
        elif isinstance(val, dict):
            for sub in ("url", "src", "href", "original"):
                u = val.get(sub)
                if u and isinstance(u, str) and u.startswith("http"):
                    urls.append(u)
    # Buscar recursivamente en el primer nivel de claves del detalle
    if not urls:
        for v in detalle.values():
            if isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        for sub in ("imageUrl", "url", "src", "photoUrl"):
                            u = item.get(sub)
                            if u and isinstance(u, str) and ("auto1" in u or "img" in u) and u.startswith("http"):
                                urls.append(u)
    return urls[:20]  # Máximo 20 fotos


def _extraer_transporte_dom(page) -> dict:
    """
    Extrae opciones de transporte directamente del DOM cuando la API no es interceptable.
    Busca textos de precio y días en la página.
    """
    try:
        # Buscar todos los textos que contengan € y días en la página
        contenido = page.inner_text("body")

        opciones = []

        # Patrones comunes en Auto1: "290 €", "5 días hábiles", etc.
        precios = re.findall(r'(\d[\d\s]*)\s*€', contenido)
        dias_patterns = re.findall(r'(\d+)\s*d[íi]as?\s*h[áa]biles?', contenido, re.IGNORECASE)

        # Buscar bloques con precio + días juntos
        bloques = re.findall(
            r'(\d[\d\.]*)\s*€[^€]{0,50}?(\d+)\s*d[íi]as?|(\d+)\s*d[íi]as?[^€]{0,50}?(\d[\d\.]*)\s*€',
            contenido,
            re.IGNORECASE
        )

        for bloque in bloques:
            precio_str = bloque[0] or bloque[3]
            dias_str = bloque[1] or bloque[2]
            if precio_str and dias_str:
                try:
                    precio = int(precio_str.replace(".", "").replace(" ", ""))
                    dias = int(dias_str)
                    if 50 < precio < 2000 and 0 < dias < 30:
                        opciones.append({"coste": precio, "dias": dias})
                except ValueError:
                    pass

        return {"opciones_dom": opciones}
    except Exception as e:
        return {"error": str(e)}


def _parsear_transporte(captured: dict, referencia: str) -> dict:
    """
    Normaliza la respuesta capturada (API o DOM) en un dict estándar.
    Devuelve la opción más barata disponible.
    """
    resultado_vacio = {
        "transporte_coste": None,
        "transporte_dias": None,
        "transporte_opcion": None,
    }

    # ── Datos desde API interceptada ─────────────────────────────
    raw = captured.get("raw")
    if raw:
        # La respuesta es una lista de objetos de coche con opciones de entrega
        items = raw if isinstance(raw, list) else [raw]
        validas = []

        for item in items:
            if not isinstance(item, dict):
                continue

            # Recorrer los tipos de entrega: truck (camión) y compoundPickup (depósito)
            # 'pickup' con cost=0 es recogida en persona — la excluimos
            for tipo in ["truck", "compoundPickup"]:
                for op in item.get(tipo, []):
                    cost_obj = op.get("transportCost", {}) or {}
                    amount_cents = cost_obj.get("amount", 0)
                    if amount_cents is None:
                        continue
                    precio_eur = round(amount_cents / 100)  # Los importes vienen en céntimos
                    duration = op.get("duration", {}) or {}
                    min_dias = duration.get("minDuration")
                    max_dias = duration.get("maxDuration")
                    dias = min_dias  # Usamos el mínimo como referencia
                    nombre = "Camión" if tipo == "truck" else "Depósito"
                    if precio_eur > 0:
                        validas.append({
                            "coste": precio_eur,
                            "dias": int(dias) if dias is not None else None,
                            "nombre": nombre,
                            "dias_max": int(max_dias) if max_dias is not None else None,
                        })

        if validas:
            mas_barata = min(validas, key=lambda x: x["coste"])
            dias_str = f"{mas_barata['dias']}-{mas_barata['dias_max']}" if mas_barata.get("dias_max") else str(mas_barata["dias"])
            logger.info(f"   🚛 Transporte {referencia}: {mas_barata['coste']}€ / {dias_str} días ({mas_barata['nombre']})")
            return {
                "transporte_coste": mas_barata["coste"],
                "transporte_dias": mas_barata["dias"],
                "transporte_opcion": mas_barata["nombre"],
            }

    # ── Datos desde DOM ───────────────────────────────────────────
    dom = captured.get("dom", {})
    opciones_dom = dom.get("opciones_dom", [])
    if opciones_dom:
        mas_barata = min(opciones_dom, key=lambda x: x["coste"])
        logger.info(f"   🚛 Transporte DOM {referencia}: {mas_barata['coste']}€ / {mas_barata['dias']} días")
        return {
            "transporte_coste": mas_barata["coste"],
            "transporte_dias": mas_barata["dias"],
            "transporte_opcion": "Económico",
        }

    logger.warning(f"   ⚠️  No se encontraron opciones de transporte para {referencia}")
    return resultado_vacio


def calcular_coste_total(precio_auto1: float, transporte_coste: float | None) -> float:
    """
    Coste total = precio Auto1 + comisión fija (500€) + transporte.
    """
    return precio_auto1 + COMISION_AUTO1 + (transporte_coste or 0)


def calcular_rentabilidad(precio_mercado: float | None, coste_total: float) -> dict:
    """
    Calcula margen y % de rentabilidad real incluyendo todos los costes.
    """
    if not precio_mercado or not coste_total:
        return {"margen_real": None, "rentabilidad_real_pct": None}

    margen = round(precio_mercado - coste_total)
    pct = round((margen / coste_total) * 100, 1)
    return {"margen_real": margen, "rentabilidad_real_pct": pct}
