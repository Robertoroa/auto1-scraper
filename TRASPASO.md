# TRASPASO — Sistema Auto1 Scraper (AutoRadar Pro)

> Documento de relevo para que cualquier cuenta de Claude (u otra persona) retome
> este proyecto sin contexto previo. Léelo entero antes de tocar nada.
> Última actualización: 2 nov 2026 — migración de Mac 2 a este MacBook.

## 1. Qué es esto

Herramienta de arbitraje de furgonetas: escrapea el stock de **Auto1** (subasta 24h
y compra inmediata) y lo compara con precios de mercado en **Wallapop** y **coches.net**
para detectar oportunidades de compra-reventa rentables. Tiene un panel web de
configuración y otro de resultados con favoritos.

## 2. Arquitectura: UNA máquina

Desde el 30 oct 2026 el sistema corre **solo en el MacBook de Roberto** (usuario
macOS `roberto`, Apple Silicon). **Mac 2 está retirada**: no se usa, no se despliega
allí y no hay que contar con su API de admin (puerto 4000) ni con Tailscale.

| | MacBook (servidor y desarrollo) |
|---|---|
| Ruta del repo | `/Users/roberto/Documents/auto1-scraper` |
| Python | `venv/` con el Python del sistema (3.9) — el código usa `from __future__ import annotations` |
| Procesos | pm2 (`auto1-scraper`, `autoradar-tunnel`) |

> ⚠️ Si alguien vuelve a encender Mac 2 con su túnel activo, Cloudflare repartirá
> el tráfico entre las dos máquinas. En Mac 2: `pm2 stop auto1-scraper autoradar-tunnel caddy-autoradar && pm2 save`.

## 3. Flujo de cambios

1. Editar el código en `/Users/roberto/Documents/auto1-scraper`.
2. `git commit` + `git push origin main` (el Mac tiene credenciales de GitHub).
3. Qué requiere cada cambio:
   - `scraper.py`, `cochesnet.py`, `wallapop.py`, `transporte.py`, `auth.py` → nada (corren como subproceso nuevo en cada scrapeo).
   - `server.py` → `pm2 restart auto1-scraper`.
   - HTML estático (`ui/index.html`, `ui/resultados.html`) → nada.

**Cómo trabaja Claude en este Mac:** la sesión enlazada al Mac da acceso a la carpeta
`Documents` y un shell Linux sin red hacia el Mac. Para ejecutar en macOS (git push,
pm2, pruebas con Playwright) Claude escribe un script `.command` en `Documents`, lo
abre con doble clic desde Finder (control del ordenador) y lee el log que deja en
`Documents/*.log`. Nunca pedir al usuario que teclee comandos.

## 4. Procesos pm2

- **`auto1-scraper`** → el panel (`server.py`), puerto **8765**, intérprete `venv/bin/python3`.
- **`autoradar-tunnel`** → `cloudflared tunnel --config ~/.cloudflared/config-autoradarpro.yml run`.
- `whatsapp-agents` → otro proyecto (Agent Force). No tocar.

Arranque automático tras reiniciar: LaunchAgent `~/Library/LaunchAgents/es.autoradarpro.pm2.plist`
(ejecuta `pm2 resurrect` al iniciar sesión). Antisueño: LaunchAgent
`es.autoradarpro.caffeinate.plist` (`caffeinate -is`; con la tapa cerrada y sin
pantalla externa el Mac se duerme igualmente).

Lanzar un scrapeo puntual (usa la config actual, no toca crons):
```bash
cd /Users/roberto/Documents/auto1-scraper && ./venv/bin/python3 -c \
"import json,urllib.request; cfg=json.load(open('config.json')); \
req=urllib.request.Request('http://127.0.0.1:8765/run-scraper-now', \
data=json.dumps(cfg).encode(), headers={'Content-Type':'application/json'}); \
print(urllib.request.urlopen(req).read().decode())"
```
Progreso: `tail -f /Users/roberto/Documents/auto1-scraper/last_run.log`

## 5. Dominios (todos vía túnel Cloudflare → este Mac)

- **https://autoradarpro.es** ← principal
- https://www.autoradarpro.es
- https://autoradar.wallaagent.es

Túnel con nombre `autoradarpro` (ID `74815dfd-424d-4e32-b795-8d3471628e12`).
Credenciales en `~/.cloudflared/` (`cert.pem`, `<ID>.json`, `config-autoradarpro.yml`).
El ingress apunta a **`http://127.0.0.1:8765`** (no `localhost`, que resolvía a IPv6
y dejaba las webs caídas). Comprobar qué máquina sirve el dominio:
`cloudflared tunnel --origincert ~/.cloudflared/cert.pem info 74815dfd-424d-4e32-b795-8d3471628e12`.

Dominio `autoradarpro.es` gestionado en Don Dominio. El dominio correcto de la org es
**wallaagent.es** (dos "a"). El panel **no tiene login**: cualquiera con la URL puede usarlo.

## 6. Componentes del código

| Archivo | Qué hace |
|---|---|
| `scraper.py` | Orquestador. Autentica en Auto1 (captura Bearer token con Playwright), descarga stock, filtra, enriquece con precios de mercado y rentabilidad. |
| `auth.py` | Sesión de Auto1: perfil persistente, autologin con credenciales de `.env`, login manual de último recurso. |
| `cochesnet.py` | Precio de mercado en coches.net (headless "nuevo" de Chrome; si bloquea, pasa solo a ventana fuera de pantalla). |
| `wallapop.py` | Precio de mercado en Wallapop. |
| `transporte.py` | Ficha del coche, coste de transporte y cálculo de rentabilidad/coste total. |
| `server.py` | Servidor HTTP del panel + endpoints (config, lanzar scrapeo, resultados, favoritos) + scheduler (APScheduler). |
| `ui/index.html` | Panel de configuración de filtros (auto + manual) y horas de scrapeo. |
| `ui/resultados.html` | Visor de resultados: tabs por canal, orden, toggle "ocultar averiados", pestaña Favoritos. |

## 7. Archivos de datos / secretos (NO están en Git — viven en el Mac)

| Archivo | Qué es | Notas |
|---|---|---|
| `.env` | `AUTO1_EMAIL` / `AUTO1_PASSWORD` | Usado por el autologin. |
| `cookies.json` | Sesión de Auto1 | Se regenera sola al hacer login. |
| `config.json` | Filtros activos y horas de scrapeo | No se commitea. |
| `favoritos.json` | Favoritos del panel | Snapshot completo de cada coche. |
| `profiles.json` | Perfiles de búsqueda guardados | |
| `google_credentials.json` | Cuenta de servicio Google Sheets | ⚠️ Clave caducada, ver pendientes. |
| `browser_profile/` | Perfil persistente de Playwright con la sesión de Auto1 | |
| `resultados/resultados_latest.json` | Último resultado | |

Copia de seguridad de lo traído de Mac 2: `~/Documents/autoradar-datos.zip` y `autoradar-tunel.zip`.

## 8. Detalles técnicos que cuesta descubrir (aprendidos a base de bugs)

- **Precios de Auto1 en DOS escalas**: euros (`15500`) o céntimos (`1645500`). Si el valor crudo `>= 100000` se divide entre 100.
- **Bearer token**: se intercepta de una request de red. `wait_until="commit"`, timeout 60s y reintentos.
- **Filtro de marcas**: la API de Auto1 espera IDs numéricos; el filtrado de marca/modelo es **client-side** en `_match_make()`.
- **Login de Auto1 (rediseño oct 2026)**: ya no existe `button.btn-primary`; los botones usan clases CSS-modules con hash y el texto "Accede". `enviar_formulario_login()` pulsa Enter en la contraseña y, si no navega, clica el submit del formulario. La URL `/es/merchant/signin` redirige a `/es/home/signin`.
- **coches.net**: bloquea el headless antiguo. Con `channel="chromium"` (headless nuevo) + user-agent normal funciona (probado 2 nov 2026). Variable `COCHESNET_MODO=ventana` fuerza el modo antiguo. `MinYear` usa el año exacto y `_modelo_coincide` evita slugs que traen la marca entera. "Super precio" siempre sale 0 (ya pasaba en Mac 2): el cálculo cae al nivel "más barato realista".
- **Filtro de averías**: `excluir_averias` (por defecto `True`).

## 9. Problemas conocidos / pendientes

- **Google Sheets**: `invalid_grant: Invalid JWT Signature`. Necesita clave nueva de la cuenta de servicio en Google Cloud (acción del usuario) → sustituir `google_credentials.json`.
- **Scrapeos automáticos**: `config.json → schedule` tiene las horas vacías; sin horas no hay scrapeos programados.
- **Login manual**: si el autologin falla, se abre una ventana de Chrome y espera 10 min a que el usuario meta la contraseña.
- **Panel sin autenticación** (ver §5).

## 10. Cómo retomar desde cero

1. Enlazar la sesión al MacBook y pedir acceso a `~/Documents`.
2. Leer este documento y `last_run.log`.
3. Verificar: `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8765/` (vía `.command`) y que https://autoradarpro.es carga.
4. Cambios → editar → commit/push desde el Mac (+ `pm2 restart auto1-scraper` si es `server.py`).
