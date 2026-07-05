"""
Servidor local del panel de control.
Sirve la UI y gestiona la comunicación con el scraper.
"""
from __future__ import annotations
import json
import os
import glob
import subprocess
import threading
import sys
import tempfile
import time
from pathlib import Path
from http.server import HTTPServer, SimpleHTTPRequestHandler
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

BASE_DIR = Path(__file__).parent
UI_DIR = BASE_DIR / "ui"
RESULTS_DIR = BASE_DIR / "resultados"
CONFIG_FILE = BASE_DIR / "config.json"
PROFILES_FILE = BASE_DIR / "profiles.json"
LOG_FILE = BASE_DIR / "last_run.log"

RESULTS_DIR.mkdir(exist_ok=True)

# Estado global del scraper
scraper_state = {
    "running": False,
    "log": [],
    "last_result": None,
    "started_at": None,
}


class PanelHandler(SimpleHTTPRequestHandler):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(UI_DIR), **kwargs)

    def log_message(self, format, *args):
        pass  # Silenciar logs del servidor

    def do_GET(self):
        if self.path == "/last-excel":
            self._handle_last_excel()
        elif self.path == "/last-log":
            try:
                if LOG_FILE.exists():
                    content = LOG_FILE.read_text(encoding="utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(content.encode("utf-8"))
                else:
                    self._json_response({"error": "Sin log todavía"})
            except Exception as e:
                self._json_response({"error": str(e)})
        elif self.path == "/scraper-status":
            self._handle_scraper_status()
        elif self.path == "/load-config":
            try:
                with open(CONFIG_FILE) as f:
                    self._json_response(json.load(f))
            except Exception:
                self._json_response({})
        elif self.path == "/scheduler-status":
            try:
                with open(CONFIG_FILE) as f:
                    cfg = json.load(f)
                h1 = cfg.get("schedule", {}).get("hora_scraping_1", "13:30")
                h2 = cfg.get("schedule", {}).get("hora_scraping_2", "20:00")
                self._json_response({"hora1": h1, "hora2": h2, "activo": True})
            except Exception:
                self._json_response({"activo": False})
        elif self.path == "/list-profiles":
            self._handle_list_profiles()
        elif self.path == "/cookie-status":
            cookies_file = BASE_DIR / "cookies.json"
            ok = cookies_file.exists()
            if ok:
                with open(cookies_file) as f:
                    data = json.load(f)
                ok = bool(data.get("MPSESSID"))
            self._json_response({"ok": ok})
        elif self.path == "/api/resultados":
            self._handle_get_resultados()
        else:
            super().do_GET()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        if self.path == "/save-config":
            self._handle_save_config()
        elif self.path == "/open-excel":
            self._handle_open_excel()
        elif self.path == "/run-scraper":
            self._handle_run_scraper()
        elif self.path == "/run-scraper-now":
            self._handle_run_scraper_now()
        elif self.path == "/login":
            self._handle_login()
        elif self.path == "/save-profile":
            self._handle_save_profile()
        elif self.path == "/delete-profile":
            self._handle_delete_profile()
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_last_excel(self):
        """Devuelve info del último Excel generado."""
        files = sorted(glob.glob(str(RESULTS_DIR / "*.xlsx")), reverse=True)
        if not files:
            self._json_response({"file": None})
            return

        latest = Path(files[0])
        stat = latest.stat()
        size_kb = round(stat.st_size / 1024, 1)

        # Leer metadata del nombre: auto1_20260615_0800.xlsx
        name = latest.stem  # auto1_20260615_0800
        parts = name.split("_")
        date_str = f"{parts[1][:4]}-{parts[1][4:6]}-{parts[1][6:]} {parts[2][:2]}:{parts[2][2:]}" if len(parts) >= 3 else "—"

        # Contar filas del Excel (aproximado por tamaño)
        cars_approx = max(1, round(stat.st_size / 5000))

        self._json_response({
            "file": latest.name,
            "path": str(latest),
            "date": date_str,
            "size": f"{size_kb} KB",
            "cars": cars_approx
        })

    def _handle_save_config(self):
        """Guarda la configuración desde el panel."""
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        config = json.loads(body)

        # Seguridad: forzar siempre channel!=batch
        if config.get("filtros", {}).get("channel") == "batch":
            config["filtros"]["channel"] = "24h"

        # Seguridad: max_coches nunca > 100
        if config.get("seguridad", {}).get("max_coches", 0) > 100:
            config["seguridad"]["max_coches"] = 100

        # Preservar campos que la UI no gestiona (google_sheets_id, etc.)
        try:
            with open(CONFIG_FILE) as f:
                existing = json.load(f)
            for key in ("google_sheets_id", "mercado"):
                if key in existing and key not in config:
                    config[key] = existing[key]
        except Exception:
            pass

        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)

        # Reprogramar scheduler con las nuevas horas
        if _reprogramar_scheduler:
            _reprogramar_scheduler()

        self._json_response({"ok": True})

    def _handle_login(self):
        """Lanza Playwright para extraer cookies — el usuario hace login en la ventana."""
        if scraper_state["running"]:
            self._json_response({"ok": False, "error": "Hay un proceso en ejecución"})
            return

        def run_login():
            scraper_state["running"] = True
            scraper_state["log"] = ["🔐 Abriendo ventana de login..."]
            try:
                sys.path.insert(0, str(BASE_DIR))
                from auth import extraer_cookies_via_login
                cookies = extraer_cookies_via_login()
                if cookies:
                    scraper_state["log"].append("✅ Login completado. Cookies guardadas.")
                    scraper_state["log"].append(f"   Claves: {list(cookies.keys())}")
                    scraper_state["log"].append("🎉 Ya puedes ejecutar el scraper.")
                else:
                    scraper_state["log"].append("❌ Login fallido o tiempo agotado.")
            except Exception as e:
                scraper_state["log"].append(f"❌ Error: {e}")
            finally:
                scraper_state["running"] = False

        threading.Thread(target=run_login, daemon=True).start()
        self._json_response({"ok": True, "message": "Abriendo ventana de login..."})

    def _handle_run_scraper(self):
        """Lanza el scraper con el config.json actual — NO lo sobreescribe.
        Para guardar cambios usar /save-config primero."""
        if scraper_state["running"]:
            self._json_response({"ok": False, "error": "El scraper ya está en ejecución"})
            return

        # Consumir el body que manda la UI pero ignorarlo — siempre usamos config.json
        length = int(self.headers.get('Content-Length', 0))
        if length:
            self.rfile.read(length)

        def run():
            scraper_state["running"] = True
            scraper_state["started_at"] = time.time()
            scraper_state["log"] = ["🚀 Iniciando scraper..."]
            try:
                python = str(BASE_DIR / "venv" / "bin" / "python3")
                proc = subprocess.Popen(
                    [python, str(BASE_DIR / "scraper.py")],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    cwd=str(BASE_DIR)
                )
                with open(LOG_FILE, "w", encoding="utf-8") as lf:
                    for line in proc.stdout:
                        line = line.rstrip()
                        if line:
                            scraper_state["log"].append(line)
                            lf.write(line + "\n")
                            lf.flush()
                proc.wait()
                done = f"✅ Proceso terminado (código {proc.returncode})"
                scraper_state["log"].append(done)
                with open(LOG_FILE, "a", encoding="utf-8") as lf:
                    lf.write(done + "\n")
            except Exception as e:
                scraper_state["log"].append(f"❌ Error: {e}")
            finally:
                scraper_state["running"] = False

        threading.Thread(target=run, daemon=True).start()
        self._json_response({"ok": True, "message": "Scraper iniciado"})

    def _handle_run_scraper_now(self):
        """Lanza un scraping puntual con config temporal — NO modifica config.json."""
        if scraper_state["running"]:
            self._json_response({"ok": False, "error": "El scraper ya está en ejecución"})
            return

        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        config_override = json.loads(body) if length else {}

        if not config_override:
            self._json_response({"ok": False, "error": "Sin configuración"})
            return

        # Seguridad: mismas restricciones que /save-config
        if config_override.get("filtros", {}).get("channel") == "batch":
            config_override["filtros"]["channel"] = "24h"
        if config_override.get("seguridad", {}).get("max_coches", 0) > 100:
            config_override["seguridad"]["max_coches"] = 100

        # Heredar campos que la UI no envía (google_sheets_id, mercado, etc.)
        try:
            with open(CONFIG_FILE) as f:
                base = json.load(f)
            for key in ("google_sheets_id", "mercado"):
                if key in base and key not in config_override:
                    config_override[key] = base[key]
        except Exception:
            pass

        # Escribir config temporal (NO toca config.json)
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", prefix="auto1_manual_",
            dir=str(BASE_DIR), delete=False
        )
        json.dump(config_override, tmp, indent=2, ensure_ascii=False)
        tmp.close()
        tmp_path = tmp.name

        def run():
            scraper_state["running"] = True
            scraper_state["started_at"] = time.time()
            scraper_state["log"] = ["⚡ Scraping puntual iniciado (config temporal, sin afectar crons)..."]
            try:
                python = str(BASE_DIR / "venv" / "bin" / "python3")
                proc = subprocess.Popen(
                    [python, str(BASE_DIR / "scraper.py"), "--config", tmp_path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    cwd=str(BASE_DIR)
                )
                with open(LOG_FILE, "w", encoding="utf-8") as lf:
                    for line in proc.stdout:
                        line = line.rstrip()
                        if line:
                            scraper_state["log"].append(line)
                            lf.write(line + "\n")
                            lf.flush()
                proc.wait()
                done = f"✅ Proceso terminado (código {proc.returncode})"
                scraper_state["log"].append(done)
                with open(LOG_FILE, "a", encoding="utf-8") as lf:
                    lf.write(done + "\n")
            except Exception as e:
                scraper_state["log"].append(f"❌ Error: {e}")
            finally:
                scraper_state["running"] = False
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

        threading.Thread(target=run, daemon=True).start()
        self._json_response({"ok": True, "message": "Scraping puntual iniciado"})

    def _handle_get_resultados(self):
        latest = RESULTS_DIR / "resultados_latest.json"
        if not latest.exists():
            self._json_response({"error": "Sin resultados aún", "canales": {}})
            return
        try:
            with open(latest, encoding="utf-8") as f:
                data = json.load(f)
            self._json_response({"canales": data})
        except Exception as e:
            self._json_response({"error": str(e), "canales": {}})

    def _handle_list_profiles(self):
        try:
            with open(PROFILES_FILE) as f:
                profiles = json.load(f)
        except Exception:
            profiles = []
        self._json_response(profiles)

    def _handle_save_profile(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        data = json.loads(body)
        name = data.get("name", "").strip()
        filtros = data.get("filtros", {})
        if not name:
            self._json_response({"ok": False, "error": "Nombre requerido"})
            return
        try:
            with open(PROFILES_FILE) as f:
                profiles = json.load(f)
        except Exception:
            profiles = []
        # Sobreescribir si ya existe
        profiles = [p for p in profiles if p.get("name") != name]
        profiles.append({"name": name, "filtros": filtros, "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S")})
        with open(PROFILES_FILE, "w") as f:
            json.dump(profiles, f, indent=2, ensure_ascii=False)
        self._json_response({"ok": True})

    def _handle_delete_profile(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        data = json.loads(body)
        name = data.get("name", "").strip()
        try:
            with open(PROFILES_FILE) as f:
                profiles = json.load(f)
        except Exception:
            profiles = []
        profiles = [p for p in profiles if p.get("name") != name]
        with open(PROFILES_FILE, "w") as f:
            json.dump(profiles, f, indent=2, ensure_ascii=False)
        self._json_response({"ok": True})

    def _handle_scraper_status(self):
        """Devuelve el estado, logs y progreso del scraper."""
        import re
        full = scraper_state["log"]
        current = total = procesados = 0
        canal = ""
        for ln in full:
            m = re.search(r"\[(24h|ip)\]\s+Procesando coche\s+(\d+)/(\d+)", ln)
            if m:
                procesados += 1
                canal = m.group(1)
                current = int(m.group(2))
                total = int(m.group(3))
        started = scraper_state.get("started_at")
        elapsed = int(time.time() - started) if started else 0
        self._json_response({
            "running": scraper_state["running"],
            "log": scraper_state["log"][-30:],  # Últimas 30 líneas
            "started_at": started,
            "elapsed": elapsed,
            "canal": canal,
            "current": current,
            "total": total,
            "procesados": procesados,
        })

    def _handle_open_excel(self):
        """Abre el Excel con la app por defecto del sistema."""
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        data = json.loads(body)
        path = data.get("path", "")

        if path and Path(path).exists():
            subprocess.Popen(["open", path])
            self._json_response({"ok": True})
        else:
            self._json_response({"ok": False, "error": "Archivo no encontrado"})

    def _json_response(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


def _cargar_filtros_perfil(nombre_perfil: str):
    """Devuelve los filtros de un perfil guardado, o None si no existe."""
    if not nombre_perfil:
        return None
    try:
        with open(PROFILES_FILE) as f:
            profiles = json.load(f)
        for p in profiles:
            if p.get("name") == nombre_perfil:
                return p.get("filtros")
    except Exception:
        pass
    return None


def lanzar_scraper_automatico(slot: str = None):
    """Lanza el scraper automáticamente desde el scheduler.
    slot: '1' o '2' para cargar el perfil asignado al cron correspondiente.
    """
    if scraper_state["running"]:
        print("⏭️  Scheduler: scraper ya en ejecución, saltando...")
        return

    # Determinar si hay perfil asignado al slot
    config_path = None
    if slot:
        try:
            with open(CONFIG_FILE) as f:
                cfg = json.load(f)
            perfil_nombre = cfg.get("schedule", {}).get(f"profile_{slot}", "")
            if perfil_nombre:
                filtros = _cargar_filtros_perfil(perfil_nombre)
                if filtros:
                    import copy
                    cfg_temporal = copy.deepcopy(cfg)
                    cfg_temporal["filtros"] = filtros
                    tmp = tempfile.NamedTemporaryFile(
                        mode="w", suffix=".json", prefix=f"auto1_cron{slot}_",
                        dir=str(BASE_DIR), delete=False
                    )
                    json.dump(cfg_temporal, tmp, indent=2, ensure_ascii=False)
                    tmp.close()
                    config_path = tmp.name
                    print(f"⏰ Scheduler slot {slot}: usando perfil '{perfil_nombre}'")
        except Exception as e:
            print(f"⚠️  Error cargando perfil para slot {slot}: {e}")

    print("⏰ Scheduler: lanzando scraper automático...")
    scraper_state["running"] = True
    scraper_state["started_at"] = time.time()
    scraper_state["log"] = ["⏰ Scraping automático iniciado por el programador..."]

    def run(cfg_path):
        try:
            python = str(BASE_DIR / "venv" / "bin" / "python3")
            cmd = [python, str(BASE_DIR / "scraper.py")]
            if cfg_path:
                cmd += ["--config", cfg_path]
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=str(BASE_DIR)
            )
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    scraper_state["log"].append(line)
                    
            proc.wait()
            scraper_state["log"].append(f"✅ Proceso terminado (código {proc.returncode})")
        except Exception as e:
            scraper_state["log"].append(f"❌ Error: {e}")
        finally:
            scraper_state["running"] = False
            if cfg_path:
                try:
                    os.unlink(cfg_path)
                except Exception:
                    pass

    threading.Thread(target=run, args=(config_path,), daemon=True).start()


def iniciar_scheduler():
    """Inicializa el scheduler con las horas del config."""
    scheduler = BackgroundScheduler()

    def cargar_y_programar():
        scheduler.remove_all_jobs()
        try:
            with open(CONFIG_FILE) as f:
                config = json.load(f)
            h1 = config.get("schedule", {}).get("hora_scraping_1", "13:30")
            h2 = config.get("schedule", {}).get("hora_scraping_2", "20:00")

            # Una hora vacía o null desactiva ese cron (scraping manual)
            programadas = []
            for h, job_id, slot in ((h1, "scraping_1", "1"), (h2, "scraping_2", "2")):
                if not h or ":" not in str(h):
                    continue
                hora, minu = str(h).split(":")
                scheduler.add_job(lanzar_scraper_automatico, CronTrigger(hour=hora, minute=minu), id=job_id, kwargs={"slot": slot})
                programadas.append(h)

            if programadas:
                print(f"⏰ Scheduler programado: {' y '.join(programadas)}")
            else:
                print("⏸️  Scheduler sin horas: scraping automático desactivado (solo manual)")
        except Exception as e:
            print(f"⚠️  Error al programar scheduler: {e}")

    cargar_y_programar()
    scheduler.start()
    print("✅ Scheduler activo — scraping automático habilitado")
    return scheduler, cargar_y_programar


# Variable global para reprogramar el scheduler cuando cambia el config
_reprogramar_scheduler = None


def run(port=8765):
    global _reprogramar_scheduler
    scheduler, reprogramar = iniciar_scheduler()
    _reprogramar_scheduler = reprogramar

    server = HTTPServer(("0.0.0.0", port), PanelHandler)
    print(f"✅ Panel disponible en http://0.0.0.0:{port}")
    print(f"   Ctrl+C para detener\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")
        scheduler.shutdown()


if __name__ == "__main__":
    run()
