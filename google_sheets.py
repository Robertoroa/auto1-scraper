"""
Módulo de integración con Google Sheets.
Gestiona 6 hojas:
  - Subasta 24h — Actual       (se sobreescribe en cada scrape)
  - Subasta 24h — Histórico    (acumula todos los scrapes)
  - Subasta 24h — Top 15       (las 15 más rentables del último scrape)
  - Compra Ahora — Actual      (se sobreescribe en cada scrape)
  - Compra Ahora — Histórico   (acumula todos los scrapes)
  - Compra Ahora — Top 15      (las 15 más rentables del último scrape)

Orden de columnas y colores fijados por el usuario:
  Cols 1-12  → sin color  (info general + coste total en azul)
  Col  12    → azul       (Coste total)
  Cols 13-16 → verde      (bloque Wallapop)
  Cols 17-19 → amarillo   (bloque coches.net)
  Cols 20-26 → sin color  (refs, links, estado)
"""

import json
import logging
from datetime import datetime
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger("auto1_scraper")

BASE_DIR = Path(__file__).parent
CREDS_FILE = BASE_DIR / "google_credentials.json"
CONFIG_FILE = BASE_DIR / "config.json"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HOJAS = {
    "24h": {
        "actual":    "Subasta 24h — Actual",
        "historico": "Subasta 24h — Histórico",
        "top15":     "Subasta 24h — Top 15",
    },
    "ip": {
        "actual":    "Compra Ahora — Actual",
        "historico": "Compra Ahora — Histórico",
        "top15":     "Compra Ahora — Top 15",
    },
}

# ── Cabecera Actual / Histórico ──────────────────────────────────────────────
# Orden fijado por el usuario. NO cambiar sin actualizar _preparar_fila también.
CABECERA = [
    "Fecha",                   # A  1
    "Marca",                   # B  2
    "Modelo",                  # C  3
    "Año",                     # D  4
    "KM",                      # E  5
    "País origen",             # F  6
    "Precio Auto1 (€)",        # G  7
    "Tipo Compra",             # H  8
    "Puja mínima (€)",         # I  9
    "Transporte (€)",          # J  10
    "Días transporte",         # K  11
    "Coste total (€)",         # L  12  ← azul
    "Precio Wallapop (€)",     # M  13  ← verde ┐
    "Precio Ref. Wallapop (€)",# N  14           │ bloque Wallapop
    "Margen Wallapop (€)",     # O  15           │
    "Rent. Wallapop (%)",      # P  16          ┘
    "Margen coches.net (€)",   # Q  17  ← amarillo ┐
    "Rent. coches.net (%)",    # R  18              │ bloque coches.net
    "Precio coches.net (€)",   # S  19             ┘
    "Ref. Wallapop",           # T  20
    "Link Wallapop",           # U  21
    "Link coches.net",         # V  22
    "Ref. Auto1",              # W  23
    "Daños motor",             # X  24
    "Revisión",                # Y  25
    "Link Auto1",              # Z  26
]

# Colores de cabecera (índices base-0, rangos [start, end))
_COLOR_AZUL     = {"red": 0.79, "green": 0.85, "blue": 0.97}
_COLOR_VERDE    = {"red": 0.85, "green": 0.92, "blue": 0.83}
_COLOR_AMARILLO = {"red": 1.00, "green": 0.95, "blue": 0.80}

_COLORES_CABECERA = [
    (11, 12, _COLOR_AZUL),      # Coste total
    (12, 16, _COLOR_VERDE),     # Precio WP, Precio Ref WP, Margen WP, Rent WP
    (16, 19, _COLOR_AMARILLO),  # Margen CN, Rent CN, Precio CN
]

# ── Cabecera Top 15 ──────────────────────────────────────────────────────────
CABECERA_TOP15 = [
    "Pos.",
    "Marca",
    "Modelo",
    "Año",
    "KM",
    "País origen",
    "Precio Auto1 (€)",
    "Transporte (€)",
    "Coste total (€)",
    "Precio Wallapop (€)",
    "Rent. Wallapop (%)",
    "Precio coches.net (€)",
    "Rent. coches.net (%)",
    "Link Wallapop",
    "Link coches.net",
    "Link Auto1",
    "Última actualización",
]


def _conectar():
    if not CREDS_FILE.exists():
        raise FileNotFoundError(
            f"No se encuentra {CREDS_FILE}. "
            "Descarga el JSON de la cuenta de servicio y ponlo ahí."
        )
    creds = Credentials.from_service_account_file(str(CREDS_FILE), scopes=SCOPES)
    return gspread.authorize(creds), creds


def _aplicar_colores_cabecera(creds, spreadsheet_id: str, sheet_id: int):
    """Aplica colores de fondo y negrita a la fila de cabecera (fila 1)."""
    requests = []

    # Primero toda la fila en negrita y sin color (blanco)
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0,
                "endRowIndex": 1,
            },
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": {"red": 1, "green": 1, "blue": 1},
                    "textFormat": {"bold": True},
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat.bold)",
        }
    })

    # Luego los bloques de color
    for start_col, end_col, color in _COLORES_CABECERA:
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 1,
                    "startColumnIndex": start_col,
                    "endColumnIndex": end_col,
                },
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": color,
                        "textFormat": {"bold": True},
                    }
                },
                "fields": "userEnteredFormat(backgroundColor,textFormat.bold)",
            }
        })

    try:
        service = build("sheets", "v4", credentials=creds)
        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests}
        ).execute()
        logger.info("✅ Colores de cabecera aplicados")
    except Exception as e:
        logger.warning(f"⚠️  No se pudieron aplicar colores: {e}")


def _crear_filter_views(creds, spreadsheet_id: str, sheet_id: int, col_rentabilidad: int, col_precio: int):
    """Crea/reemplaza filter views en la hoja Actual via Sheets API v4.
    col_rentabilidad y col_precio son índices base-0.
    """
    try:
        service = build("sheets", "v4", credentials=creds)
        spreadsheets = service.spreadsheets()

        meta = spreadsheets.get(spreadsheetId=spreadsheet_id).execute()
        sheets_meta = meta.get("sheets", [])
        sheet_meta = next((s for s in sheets_meta if s["properties"]["sheetId"] == sheet_id), None)

        delete_requests = []
        if sheet_meta:
            for fv in sheet_meta.get("filterViews", []):
                delete_requests.append({"deleteFilterView": {"filterId": fv["filterViewId"]}})

        add_requests = [
            {
                "addFilterView": {
                    "filter": {
                        "title": "Por Rentabilidad ↓",
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "startColumnIndex": 0,
                        },
                        "sortSpecs": [{"dimensionIndex": col_rentabilidad, "sortOrder": "DESCENDING"}],
                    }
                }
            },
            {
                "addFilterView": {
                    "filter": {
                        "title": "Por Precio ↑",
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "startColumnIndex": 0,
                        },
                        "sortSpecs": [{"dimensionIndex": col_precio, "sortOrder": "ASCENDING"}],
                    }
                }
            },
        ]

        spreadsheets.batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": delete_requests + add_requests}
        ).execute()
        logger.info("✅ Filter views creadas: 'Por Rentabilidad ↓' y 'Por Precio ↑'")
    except Exception as e:
        logger.warning(f"⚠️  No se pudieron crear filter views: {e}")


def _obtener_spreadsheet_id():
    with open(CONFIG_FILE) as f:
        config = json.load(f)
    sid = config.get("google_sheets_id", "")
    if not sid:
        raise ValueError("Falta 'google_sheets_id' en config.json.")
    return sid


def _score_rentabilidad(coche: dict) -> float:
    wp  = coche.get("rentabilidad_wallapop_pct")
    cn  = coche.get("rentabilidad_cochesnet_pct")
    vals = [v for v in [wp, cn] if v is not None]
    return max(vals) if vals else -999


def _ordenar_por_rentabilidad(resultados: list) -> list:
    return sorted(resultados, key=_score_rentabilidad, reverse=True)


def _preparar_fila(coche: dict, fecha: str) -> list:
    """
    Devuelve los valores en el mismo orden que CABECERA.
    Cualquier cambio en CABECERA debe reflejarse aquí.
    """
    precio_auto1  = coche.get("precio_auto1", 0) or 0
    transporte    = coche.get("transporte_coste", "")
    dias_transp   = coche.get("transporte_dias", "")
    coste_total   = coche.get("coste_total", "")
    tipo_compra   = coche.get("tipo_compra", "")
    puja_minima   = coche.get("puja_minima", "") or ""
    ref           = coche.get("referencia", "")
    url_auto1     = f"https://www.auto1.com/es/app/merchant/car/{ref}" if ref else ""

    # Wallapop
    precio_wp     = coche.get("precio_mercado_wallapop") or ""
    ref_precio_wp = coche.get("wallapop_ref_precio", "")
    margen_wp     = coche.get("margen_wallapop", "")
    rent_wp       = coche.get("rentabilidad_wallapop_pct")
    ref_titulo    = coche.get("wallapop_ref_titulo", "")
    url_wallapop  = coche.get("wallapop_ref_url", "")

    # coches.net
    margen_cn     = coche.get("margen_cochesnet", "")
    rent_cn       = coche.get("rentabilidad_cochesnet_pct")
    precio_cn     = coche.get("precio_mercado_cochesnet") or ""
    url_cochesnet = coche.get("cochesnet_url", "")

    return [
        fecha,                                                                 # A  Fecha
        coche.get("marca", ""),                                                # B  Marca
        coche.get("modelo", ""),                                               # C  Modelo
        coche.get("año", ""),                                                  # D  Año
        coche.get("km", 0),                                                    # E  KM
        coche.get("pais", ""),                                                 # F  País origen
        precio_auto1,                                                          # G  Precio Auto1
        tipo_compra,                                                           # H  Tipo Compra
        puja_minima,                                                           # I  Puja mínima
        transporte if transporte != "" else "",                                # J  Transporte
        dias_transp if dias_transp != "" else "",                              # K  Días transporte
        coste_total if coste_total != "" else "",                              # L  Coste total  ← azul
        precio_wp,                                                             # M  Precio Wallapop     ┐
        ref_precio_wp or "",                                                   # N  Precio Ref. WP      │ verde
        margen_wp if margen_wp != "" else "",                                  # O  Margen Wallapop     │
        f"{rent_wp}%" if rent_wp is not None else "",                          # P  Rent. Wallapop     ┘
        margen_cn if margen_cn != "" else "",                                  # Q  Margen coches.net  ┐
        f"{rent_cn}%" if rent_cn is not None else "",                          # R  Rent. coches.net   │ amarillo
        precio_cn,                                                             # S  Precio coches.net  ┘
        ref_titulo,                                                            # T  Ref. Wallapop
        url_wallapop,                                                          # U  Link Wallapop
        url_cochesnet,                                                         # V  Link coches.net
        ref,                                                                   # W  Ref. Auto1
        "Sí" if coche.get("danos_motor") else (                               # X  Daños motor
            "No" if coche.get("danos_motor") is False else "N/D"),
        "⚠️ Sí" if coche.get("requiere_revision") else "OK",                  # Y  Revisión
        url_auto1,                                                             # Z  Link Auto1
    ]


def _preparar_fila_top15(pos: int, coche: dict, fecha: str) -> list:
    precio_auto1  = coche.get("precio_auto1", 0) or 0
    transporte    = coche.get("transporte_coste", "")
    coste_total   = coche.get("coste_total", "")
    ref           = coche.get("referencia", "")
    url_auto1     = f"https://www.auto1.com/es/app/merchant/car/{ref}" if ref else ""
    url_wallapop  = coche.get("wallapop_ref_url", "")
    url_cochesnet = coche.get("cochesnet_url", "")
    precio_wp     = coche.get("precio_mercado_wallapop") or ""
    rent_wp       = coche.get("rentabilidad_wallapop_pct")
    precio_cn     = coche.get("precio_mercado_cochesnet") or ""
    rent_cn       = coche.get("rentabilidad_cochesnet_pct")

    return [
        pos,
        coche.get("marca", ""),
        coche.get("modelo", ""),
        coche.get("año", ""),
        coche.get("km", 0),
        coche.get("pais", ""),
        precio_auto1,
        transporte if transporte != "" else "",
        coste_total if coste_total != "" else "",
        precio_wp,
        f"{rent_wp}%" if rent_wp is not None else "",
        precio_cn,
        f"{rent_cn}%" if rent_cn is not None else "",
        url_wallapop,
        url_cochesnet,
        url_auto1,
        fecha,
    ]


def _asegurar_hoja(spreadsheet, nombre: str, cabecera: list, cols: int = 30) -> gspread.Worksheet:
    try:
        return spreadsheet.worksheet(nombre)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=nombre, rows=1000, cols=cols)
        ws.append_row(cabecera, value_input_option="USER_ENTERED")
        return ws


def actualizar_sheets(resultados: list, canal: str):
    """
    Actualiza Google Sheets con los resultados del scrape.
    - Hoja Actual:    se reemplaza completamente, ordenada por rentabilidad.
    - Hoja Histórico: se añaden los coches del scrape actual.
    - Hoja Top 15:    se reemplaza con las 15 más rentables.
    """
    if not resultados:
        logger.info("ℹ️  Sin resultados para subir a Sheets.")
        return

    canal_key = canal if canal in HOJAS else "24h"
    nombres = HOJAS[canal_key]
    fecha = datetime.now().strftime("%d/%m/%Y %H:%M")

    try:
        gc, creds = _conectar()
        sheet_id = _obtener_spreadsheet_id()
        spreadsheet = gc.open_by_key(sheet_id)
        logger.info(f"✅ Conectado a Google Sheets: {spreadsheet.title}")
    except Exception as e:
        logger.error(f"❌ Error conectando a Google Sheets: {e}")
        return

    ordenados = _ordenar_por_rentabilidad(resultados)

    # ── Hoja ACTUAL ──────────────────────────────────────────────────────────
    try:
        ws_actual = _asegurar_hoja(spreadsheet, nombres["actual"], CABECERA)
        ws_actual.clear()
        ws_actual.append_row(CABECERA, value_input_option="USER_ENTERED")
        filas = [_preparar_fila(c, fecha) for c in ordenados]
        if filas:
            ws_actual.append_rows(filas, value_input_option="USER_ENTERED")
        logger.info(f"✅ Hoja '{nombres['actual']}' — {len(filas)} coches.")

        # Colores de cabecera
        _aplicar_colores_cabecera(creds, sheet_id, ws_actual.id)

        # Filter views (índices base-0)
        col_rent  = CABECERA.index("Rent. Wallapop (%)")
        col_precio = CABECERA.index("Precio Auto1 (€)")
        _crear_filter_views(creds, sheet_id, ws_actual.id, col_rent, col_precio)
    except Exception as e:
        logger.error(f"❌ Error en hoja Actual: {e}")

    # ── Hoja HISTÓRICO ───────────────────────────────────────────────────────
    try:
        ws_hist = _asegurar_hoja(spreadsheet, nombres["historico"], CABECERA)
        filas_hist = [_preparar_fila(c, fecha) for c in ordenados]
        if filas_hist:
            ws_hist.append_rows(filas_hist, value_input_option="USER_ENTERED")
        logger.info(f"✅ Hoja '{nombres['historico']}' — añadidos {len(filas_hist)} coches.")
    except Exception as e:
        logger.error(f"❌ Error en hoja Histórico: {e}")

    # ── Hoja TOP 15 ──────────────────────────────────────────────────────────
    try:
        ws_top = _asegurar_hoja(spreadsheet, nombres["top15"], CABECERA_TOP15)
        ws_top.clear()
        ws_top.append_row(CABECERA_TOP15, value_input_option="USER_ENTERED")

        con_precio = [
            c for c in ordenados
            if (c.get("precio_mercado_wallapop") or c.get("precio_mercado_cochesnet"))
            and (c.get("rentabilidad_wallapop_pct") is not None or c.get("rentabilidad_cochesnet_pct") is not None)
        ]
        top15 = con_precio[:15]
        filas_top = [_preparar_fila_top15(i + 1, c, fecha) for i, c in enumerate(top15)]
        if filas_top:
            ws_top.append_rows(filas_top, value_input_option="USER_ENTERED")
        logger.info(f"✅ Hoja '{nombres['top15']}' — Top {len(filas_top)} oportunidades.")
    except Exception as e:
        logger.error(f"❌ Error en hoja Top 15: {e}")
