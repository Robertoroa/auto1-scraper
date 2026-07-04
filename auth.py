"""
MÓDULO DE AUTENTICACIÓN
Perfil persistente + autologin automático desde credenciales locales.
La sesión dura semanas con el perfil persistente. Si expira, reloguea solo.
"""

import json
import time
import logging
import os
from pathlib import Path

logger = logging.getLogger("auto1_scraper")

COOKIES_FILE = Path(__file__).parent / "cookies.json"
BROWSER_PROFILE_DIR = Path(__file__).parent / "browser_profile"
CREDS_FILE = Path(__file__).parent / ("." + "env")
AUTO1_LOGIN_URL = "https://www.auto1.com/es/home"
AUTO1_DEALER_URL = "https://www.auto1.com/es/app/merchant/cars"
AUTO1_SIGNIN_URL = "https://www.auto1.com/es/merchant/signin"


def _cargar_credenciales():
    """Lee AUTO1_EMAIL y AUTO1_PASSWORD del archivo de credenciales."""
    creds = {}
    if CREDS_FILE.exists():
        for line in CREDS_FILE.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                creds[k.strip()] = v.strip()
    email = creds.get("AUTO1_EMAIL") or os.environ.get("AUTO1_EMAIL")
    password = creds.get("AUTO1_PASSWORD") or os.environ.get("AUTO1_PASSWORD")
    return email, password


def _guardar_cookies_desde_context(context):
    """Extrae y guarda las cookies importantes del contexto."""
    cookies = context.cookies()
    cookies_importantes = {
        c["name"]: c["value"]
        for c in cookies
        if c["name"] in ["MPSESSID", "loggedQoS", "xsrf_token", "isUserLogged", "hl", "auto1_locale"]
    }
    if cookies_importantes.get("MPSESSID"):
        with open(COOKIES_FILE, "w") as f:
            json.dump(cookies_importantes, f, indent=2)
        return cookies_importantes
    return None


def _sesion_activa(context):
    """Devuelve True si el contexto tiene sesión activa."""
    cookies = context.cookies()
    return any(c["name"] == "isUserLogged" and c["value"] == "true" for c in cookies)


def autologin():
    """
    Login automático usando credenciales locales.
    Usa perfil persistente para que la sesión dure semanas.
    """
    from playwright.sync_api import sync_playwright

    email, password = _cargar_credenciales()
    if not email or not password:
        logger.warning("No hay credenciales guardadas — se necesita login manual")
        return None

    logger.info("Autologin automatico iniciando...")
    BROWSER_PROFILE_DIR.mkdir(exist_ok=True)

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(BROWSER_PROFILE_DIR),
                headless=True,
            )
            page = context.pages[0] if context.pages else context.new_page()

            page.goto(AUTO1_SIGNIN_URL, wait_until="domcontentloaded", timeout=20000)
            time.sleep(2)

            current_url = page.url
            logger.info(f"   URL: {current_url[:60]}")

            # Si ya estamos dentro, no hace falta loguearse
            if any(x in current_url for x in ["merchant/cars", "/app/"]):
                logger.info("Sesion ya activa en perfil persistente")
                cookies = _guardar_cookies_desde_context(context)
                context.close()
                return cookies

            # Cerrar banner de cookies si aparece
            try:
                cookie_btn = page.locator("button:has-text('ACEPTAR')")
                if cookie_btn.is_visible(timeout=3000):
                    cookie_btn.click()
                    time.sleep(0.5)
            except Exception:
                pass

            # Rellenar formulario
            page.wait_for_selector("#login-email", timeout=10000)

            # Hay 2 inputs con mismo ID — usar el primero (formulario principal, no modal)
            email_input = page.locator("#login-email").first
            email_input.fill(email, force=True)
            time.sleep(0.5)

            pass_input = page.locator("#login-password").first
            pass_input.fill(password, force=True)
            time.sleep(0.5)

            # Botón visible "Acceder" — force=True para saltar comprobaciones de visibilidad
            submit = page.locator("button.btn-primary:visible").first
            submit.click(force=True)

            # Esperar redirección hasta 30s
            for _ in range(30):
                time.sleep(1)
                url = page.url
                if any(x in url for x in ["merchant/cars", "/app/", "dashboard"]):
                    logger.info(f"Autologin exitoso: {url[:60]}")
                    cookies = _guardar_cookies_desde_context(context)
                    context.close()
                    return cookies
                if _sesion_activa(context):
                    logger.info("Autologin exitoso (cookie detectada)")
                    cookies = _guardar_cookies_desde_context(context)
                    context.close()
                    return cookies

            logger.error("Autologin fallo — timeout esperando redireccion")
            context.close()
            return None

    except Exception as e:
        logger.error(f"Error en autologin: {e}")
        return None


def refrescar_sesion_silencioso():
    """
    Abre el perfil persistente en headless para refrescar la sesion.
    Si expiro, intenta autologin automatico.
    """
    from playwright.sync_api import sync_playwright

    if not BROWSER_PROFILE_DIR.exists():
        return autologin()

    needs_login = False
    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(BROWSER_PROFILE_DIR),
                headless=True,
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(AUTO1_DEALER_URL, wait_until="domcontentloaded", timeout=20000)
            time.sleep(2)

            current_url = page.url
            cookies = _guardar_cookies_desde_context(context)
            context.close()

            on_merchant = any(x in current_url for x in ["merchant", "/app/"])
            is_logged = cookies and cookies.get("isUserLogged") == "true"

            if on_merchant and is_logged:
                logger.info("Sesion refrescada automaticamente")
                return cookies
            else:
                logger.info("Sesion expirada en perfil — intentando autologin...")
                needs_login = True

    except Exception as e:
        logger.warning(f"No se pudo refrescar sesion: {e}")
        needs_login = True

    if needs_login:
        return autologin()
    return None


def extraer_cookies_via_login():
    """
    Primero intenta autologin automatico.
    Si falla, abre ventana para login manual.
    """
    from playwright.sync_api import sync_playwright

    # Autologin primero
    result = autologin()
    if result:
        return result

    # Fallback: login manual
    BROWSER_PROFILE_DIR.mkdir(exist_ok=True)

    print("\n" + "="*60)
    print("LOGIN MANUAL — HAZ LOGIN EN LA VENTANA QUE SE ABRE")
    print("="*60 + "\n")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_PROFILE_DIR),
            headless=False,
            args=["--window-size=1280,800", "--window-position=100,100"],
            viewport={"width": 1280, "height": 800},
        )

        page = context.pages[0] if context.pages else context.new_page()
        page.goto(AUTO1_LOGIN_URL)

        print("Haz login en la ventana que acaba de abrirse...")

        logged_in = False
        for _ in range(600):
            time.sleep(1)
            try:
                current_url = page.url
                if any(x in current_url for x in ["merchant", "/app/", "dashboard"]):
                    logged_in = True
                    print(f"Login detectado: {current_url[:80]}")
                    break
                if _sesion_activa(context):
                    logged_in = True
                    print("Login detectado (cookie)")
                    break
            except Exception:
                pass

        if not logged_in:
            print("Timeout sin detectar login.")
            context.close()
            return None

        time.sleep(2)
        cookies = _guardar_cookies_desde_context(context)
        context.close()
        return cookies


def cargar_cookies():
    """
    1. Refresca desde perfil persistente (con autologin si expira)
    2. Si no hay perfil, carga desde cookies guardadas
    3. Ultimo recurso: login manual
    """
    refreshed = refrescar_sesion_silencioso()
    if refreshed:
        print("Cookies cargadas correctamente.")
        return refreshed

    if COOKIES_FILE.exists():
        with open(COOKIES_FILE, "r") as f:
            cookies = json.load(f)
        if cookies.get("MPSESSID") and cookies.get("isUserLogged") == "true":
            print("Cookies cargadas desde archivo.")
            return cookies

    print("Sesion expirada. Iniciando login...")
    return extraer_cookies_via_login()


def cookies_a_header(cookies: dict) -> dict:
    """Convierte el dict de cookies al formato de header HTTP."""
    return {
        "Cookie": "; ".join([f"{k}={v}" for k, v in cookies.items()])
    }
