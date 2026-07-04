"""
MÓDULO DE SEGURIDAD — Se carga PRIMERO en todo el sistema.
Bloquea cualquier llamada que pueda implicar una compra o puja.
"""

# ============================================================
# LISTA NEGRA DE URLs — NUNCA llamar a estas rutas
# ============================================================
BLOCKED_URL_FRAGMENTS = [
    "buy", "bid", "order", "purchase", "checkout",
    "comprar", "pujar", "reservar", "pagar", "confirmar",
    "payment", "invoice", "factura", "cart", "basket",
    "place-order", "place_order", "submit-order",
    "/order/", "/bid/", "/buy/", "/purchase/",
]

# ============================================================
# MÉTODOS HTTP PERMITIDOS
# ============================================================
ALLOWED_METHODS = ["GET", "POST"]

# ============================================================
# ENDPOINTS POST PERMITIDOS (whitelist estricta)
# Solo estos POSTs son seguros — todos los demás bloqueados
# ============================================================
ALLOWED_POST_ENDPOINTS = [
    "/v1/car-search/cars/search/",
    "/v1/car-search/cars/recommended/",
    "/cars/search",   # cubre /v1/car-search/{dealer_uuid}/cars/search
]

# ============================================================
# LÍMITE DURO DE COCHES
# ============================================================
MAX_CARS = 100

# ============================================================
# MODO SIMULACIÓN — Se lee del config.json
# ============================================================
def _leer_dry_run():
    import json
    from pathlib import Path
    try:
        config = json.loads((Path(__file__).parent / "config.json").read_text())
        return config.get("seguridad", {}).get("dry_run", True)
    except Exception:
        return True  # Si falla, modo seguro por defecto

DRY_RUN = _leer_dry_run()


def check_url_safe(method: str, url: str) -> None:
    """
    Verifica que una URL es segura antes de llamarla.
    Lanza excepción si detecta algo peligroso.
    SIEMPRE llamar esto antes de cualquier request.
    """
    method = method.upper()

    # 1. Método permitido
    if method not in ALLOWED_METHODS:
        raise SecurityError(f"❌ MÉTODO BLOQUEADO: {method} no está permitido")

    # 2. Palabras prohibidas en la URL
    url_lower = url.lower()
    for fragment in BLOCKED_URL_FRAGMENTS:
        if fragment in url_lower:
            raise SecurityError(
                f"❌ URL BLOQUEADA: contiene '{fragment}'\n"
                f"   URL: {url}\n"
                f"   Posible riesgo de compra/puja. Operación cancelada."
            )

    # 3. Para POST, verificar que está en la whitelist
    if method == "POST":
        allowed = any(endpoint in url for endpoint in ALLOWED_POST_ENDPOINTS)
        if not allowed:
            raise SecurityError(
                f"❌ POST BLOQUEADO: endpoint no está en la whitelist\n"
                f"   URL: {url}\n"
                f"   Solo se permiten POSTs a: {ALLOWED_POST_ENDPOINTS}"
            )

    return True


def safe_request(session, method: str, url: str, **kwargs):
    """
    Wrapper seguro para requests. Verifica seguridad y loguea.
    Usar SIEMPRE en lugar de session.get() o session.post() directamente.
    """
    import logging
    from datetime import datetime

    logger = logging.getLogger("auto1_scraper")

    # Verificación de seguridad PRIMERO
    check_url_safe(method, url)

    # Log antes de ejecutar
    logger.info(f"[{datetime.now().isoformat()}] {method} {url}")

    # Si estamos en DRY_RUN, no llamamos nada
    if DRY_RUN:
        logger.warning(f"🔵 DRY_RUN ACTIVO — Llamada simulada: {method} {url}")
        return DryRunResponse(method, url)

    # Ejecutar la llamada real
    response = session.request(method, url, **kwargs)

    logger.info(f"   → Status: {response.status_code}")
    return response


class SecurityError(Exception):
    """Error de seguridad — operación bloqueada."""
    pass


class DryRunResponse:
    """Respuesta simulada para modo DRY_RUN."""
    def __init__(self, method, url):
        self.method = method
        self.url = url
        self.status_code = 200
        self._json = {"cars": [], "totalCount": 0, "dry_run": True}

    def json(self):
        return self._json

    def raise_for_status(self):
        pass
