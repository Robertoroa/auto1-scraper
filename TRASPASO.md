# TRASPASO — Sistema Auto1 Scraper

> Documento de relevo para que cualquier cuenta de Claude (u otra persona) retome
> este proyecto sin contexto previo. Léelo entero antes de tocar nada.

## 1. Qué es esto

Herramienta de arbitraje de furgonetas: escrapea el stock de **Auto1** (subasta 24h
y compra inmediata) y lo compara con precios de mercado en **Wallapop** y **coches.net**
para detectar oportunidades de compra-reventa rentables. Tiene un panel web de
configuración y otro de resultados con favoritos.

## 2. Arquitectura: dos máquinas

| | Mac 1 (desarrollo) | Mac 2 (servidor 24/7) |
|---|---|---|
| Rol | Editar código | Ejecutar el sistema |
| Tailscale IP | — | **100.116.208.4** |
| Ruta del repo | `~/Documents/auto1-scraper` | `/Users/robertoderoaperez/auto1-scraper` |

**El sistema real corre SOLO en Mac 2.** Mac 1 es solo el editor.

## 3. Flujo de despliegue (SIEMPRE así)

1. Editar código en Mac 1.
2. `git commit` + `git push origin main`.
3. Desplegar en Mac 2 vía API de admin (NUNCA pedir al usuario que teclee en Mac 2).

**Qué requiere cada cambio:**
- `scraper.py`, `cochesnet.py`, `wallapop.py`, `transporte.py` → solo `git pull` (corren como subproceso nuevo en cada scrapeo).
- `server.py` → `git pull` **+ `pm2 restart auto1-scraper`** (es el proceso del panel).
- HTML estático (`ui/index.html`, `ui/resultados.html`) → solo `git pull`.

## 4. API de admin de Mac 2 (cómo ejecutar comandos remotos)

```bash
# 1. Login → JWT
TOKEN=$(curl -s -X POST http://100.116.208.4:4000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"<ADMIN_EMAIL>","password":"<ADMIN_PASS>"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

# 2. Ejecutar cualquier comando en Mac 2
curl -s -X POST http://100.116.208.4:4000/api/admin/exec \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"cmd":"cd /Users/robertoderoaperez/auto1-scraper && git pull origin main"}'
```

> Las credenciales admin (`<ADMIN_EMAIL>` / `<ADMIN_PASS>`) están en el `CLAUDE.md`
> global del usuario y en el checklist de credenciales del traspaso. NO se guardan aquí.

## 5. Procesos pm2 en Mac 2

- **`auto1-scraper`** → el panel (`server.py`), puerto **8765**. Este es el nuestro.
- `wallaagent-admin` → API de admin, puerto 4000.
- `autoradar-tunnel`, `caddy-autoradar` → túnel Cloudflare + proxy.
- (Otros procesos `wallapop-agent-*`, `wallaagent-*` son de otros proyectos; no tocar.)

Lanzar un scrapeo puntual desde Mac 2 (usa la config actual, no toca crons):
```bash
cd /Users/robertoderoaperez/auto1-scraper && ./venv/bin/python3 -c \
"import json,urllib.request; cfg=json.load(open('config.json')); \
req=urllib.request.Request('http://localhost:8765/run-scraper-now', \
data=json.dumps(cfg).encode(), headers={'Content-Type':'application/json'}); \
print(urllib.request.urlopen(req).read().decode())"
```
Seguir el progreso: `tail -f /Users/robertoderoaperez/auto1-scraper/last_run.log`

## 6. Dominios (todos apuntan a Mac 2 vía Cloudflare)

- **https://autoradarpro.es** ← principal
- https://www.autoradarpro.es
- https://autoradar.wallaagent.es

El panel sirve `ui/index.html` (config) y `ui/resultados.html` (resultados + favoritos).
Dominio `autoradarpro.es` gestionado en Don Dominio (credenciales en la memoria del usuario).

> Ojo: el dominio correcto de la org es **wallaagent.es** (dos "a"). `wallagent.es` no resuelve.

## 7. Componentes del código

| Archivo | Qué hace |
|---|---|
| `scraper.py` | Orquestador. Autentica en Auto1 (captura Bearer token con Playwright), descarga stock, filtra, enriquece con precios de mercado y rentabilidad. |
| `cochesnet.py` | Precio de mercado en coches.net (Playwright `headless=False` fuera de pantalla por detección de bots). |
| `wallapop.py` | Precio de mercado en Wallapop. |
| `transporte.py` | Ficha del coche, coste de transporte y cálculo de rentabilidad/coste total. |
| `auth.py` | Carga de cookies de sesión Auto1. |
| `server.py` | Servidor HTTP del panel + endpoints (config, lanzar scrapeo, resultados, favoritos). |
| `ui/index.html` | Panel de configuración de filtros (auto + manual). |
| `ui/resultados.html` | Visor de resultados: tabs por canal, orden, toggle "ocultar averiados", pestaña Favoritos. |

## 8. Archivos de datos / secretos (NO están en Git — viven en Mac 2)

| Archivo | Qué es | Notas |
|---|---|---|
| `cookies.json` | Sesión de Auto1 | Caduca; hay que renovarla cuando expira (login). |
| `config.json` | Filtros activos | **Difiere entre Mac 1 y Mac 2.** No se commitea para no pisar la de Mac 2. El scraper usa defaults si falta un campo. |
| `favoritos.json` | Favoritos del panel (lado servidor) | Snapshot completo de cada coche; persiste entre scrapeos. |
| `profiles.json` | Perfiles de búsqueda guardados | |
| `.env` | Variables de entorno | |
| `google_credentials.json` | Cuenta de servicio Google Sheets | ⚠️ Ver problemas conocidos. |
| `cochesnet_profile/`, `browser_profile/` | Perfiles persistentes de Playwright | |

## 9. Detalles técnicos que cuesta descubrir (aprendidos a base de bugs)

- **Precios de Auto1 vienen en DOS escalas**: euros (`15500` = 15.500 €) o céntimos
  (`1645500` = 16.455 €). Regla: si el valor crudo `>= 100000` se divide entre 100.
- **Captura del Bearer token**: se intercepta de una request de red, no de la carga de
  la página. Usar `wait_until="commit"`, timeout 60s y reintentos con recarga. NO depender
  de `domcontentloaded`.
- **Filtro de marcas**: la API de Auto1 espera IDs numéricos, NO nombres. Enviar
  `{"make":"Volkswagen"}` en el body devuelve 0 resultados. El filtrado de marca/modelo
  se hace **client-side** en `_match_make()`. Por eso `construir_body_busqueda()` NO mete
  `makes` en el body: se descarga el stock comercial completo y se filtra en local.
- **coches.net MinYear** usa el año exacto del coche (no año − 3). Y hay un filtro de
  modelo (`_modelo_coincide`) como red de seguridad contra slugs que devuelven la marca
  entera (ej. VW T6 → antes traía Golf/Polo y daba precios absurdos).
- **Filtro de averías**: flag `excluir_averias` (por defecto `True`) descarta coches con
  daño en motor o avería en prueba dinámica. Checkbox en el panel de config.

## 10. Problemas conocidos / pendientes

- **Google Sheets**: la cuenta de servicio da `invalid_grant: Invalid JWT Signature`.
  Necesita una **clave nueva** de la consola de Google Cloud (acción del usuario).
- **Túnel Cloudflare** apunta a `localhost:8765`, que resuelve a IPv6 (`[::1]`) mientras
  el panel escucha en IPv4 (`0.0.0.0`). Si el panel se reinicia mal, puede quedar
  "online" en pm2 sin tener el puerto abierto → las webs devuelven error. Fix recomendado
  (pendiente): cambiar el ingress del túnel de `localhost` a `127.0.0.1`.
- **Mac 1 tiene su propio panel + túnel** (`panel.autoradarpro.es`) arrancados por agentes
  launchd. Puede abrir navegador en Mac 1 en vez de Mac 2. Se ofreció desactivar los
  agentes launchd (pendiente de confirmación del usuario).

## 11. Cómo retomar desde cero (checklist para la cuenta nueva)

1. Clonar el repo: `git clone https://github.com/Robertoroa/auto1-scraper.git`
2. Tener a mano las **credenciales** (ver checklist aparte — no van en Git):
   admin de Mac 2, acceso SSH a Mac 2, Don Dominio.
3. Verificar que Mac 2 responde: `curl http://100.116.208.4:4000/api/auth/login ...`
4. Para cualquier cambio → editar → commit/push → `git pull` en Mac 2 (+ restart si es `server.py`).
5. NO hace falta ejecutar nada en local: el sistema corre en Mac 2.
