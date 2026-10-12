"""Backend local de Sellium: Ollama + herramientas controladas del equipo."""
from __future__ import annotations

import asyncio
import ctypes
from datetime import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from urllib.parse import quote_plus
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from backend.voice import VoiceUnavailable, synthesize_wav, transcribe


PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = Path(os.getenv("AGENT_WORKSPACE", str(PROJECT_ROOT))).resolve()
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")
# El asistente se ejecuta solamente en localhost. Puede desactivarse con false.
ENABLE_APP_LAUNCH = os.getenv("ENABLE_APP_LAUNCH", "true").lower() == "true"
MAX_HISTORY = 20
IGNORED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__"}
MEMORIES_FILE = WORKSPACE / "data" / "memories.json"
_memories_lock = threading.Lock()
_cpu_sample: tuple[int, int] | None = None
_gpu_cache: tuple[float | None, float] = (None, 0.0)
KNOWN_APPS = {
    "calculadora": ["calc.exe"], "calculator": ["calc.exe"],
    "bloc de notas": ["notepad.exe"], "notepad": ["notepad.exe"],
    "explorador": ["explorer.exe"], "explorer": ["explorer.exe"],
    "paint": ["mspaint.exe"], "terminal": ["wt.exe"],
    "powershell": ["powershell.exe"], "administrador de tareas": ["taskmgr.exe"],
    "configuración": ["ms-settings:"], "settings": ["ms-settings:"],
    "visual studio code": ["code"], "vscode": ["code"], "code": ["code"],
}

app = FastAPI(title="Sellium Local Agent", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    # El servidor Vite usa un puerto variable (normalmente 5173). Para CORS,
    # ``http://localhost`` y ``http://localhost:5173`` son orígenes distintos.
    # Aceptamos sólo loopback, además de los esquemas propios de Tauri.
    allow_origins=[
        "http://localhost",
        "http://127.0.0.1",
        "http://tauri.localhost",
        "tauri://localhost",
        "null",
    ],
    allow_origin_regex=(
        r"^https?://(?:localhost|127\.0\.0\.1)(?::\d+)?$"
        r"|^https?://tauri\.localhost(?::\d+)?$"
    ),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12_000)
    history: list[dict[str, str]] = Field(default_factory=list)


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=6_000)


class _FileTime(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_ulong), ("dwHighDateTime", ctypes.c_ulong)]


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _filetime_value(value: _FileTime) -> int:
    return (value.dwHighDateTime << 32) + value.dwLowDateTime


def _cpu_percent() -> float | None:
    """Calcula CPU total de Windows con dos muestras, sin dependencias externas."""
    global _cpu_sample
    if sys.platform != "win32":
        return None
    idle, kernel, user = _FileTime(), _FileTime(), _FileTime()
    if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return None
    total = _filetime_value(kernel) + _filetime_value(user)
    idle_value = _filetime_value(idle)
    previous = _cpu_sample
    _cpu_sample = (total, idle_value)
    if not previous or total <= previous[0]:
        return 0.0
    busy = 1 - ((idle_value - previous[1]) / (total - previous[0]))
    return round(max(0, min(1, busy)) * 100, 1)


def _gpu_percent() -> float | None:
    """Lee el contador GPU de Windows cuando el controlador lo expone.

    No todos los drivers publican esta clase WMI; en ese caso el frontend recibe
    ``null`` y puede indicar que la métrica no está disponible.
    """
    global _gpu_cache
    cached, cached_at = _gpu_cache
    if time.monotonic() - cached_at < 4:
        return cached
    if sys.platform != "win32":
        return None
    command = (
        "Get-CimInstance -Namespace root\\cimv2 -ClassName "
        "Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine -ErrorAction Stop | "
        "Measure-Object -Property UtilizationPercentage -Sum | Select-Object -ExpandProperty Sum"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3, check=True,
        )
        value = float(result.stdout.strip().replace(",", "."))
        # Los motores pueden solaparse; el panel representa un porcentaje legible.
        cached = round(max(0, min(100, value)), 1)
    except (OSError, subprocess.SubprocessError, ValueError):
        cached = None
    _gpu_cache = (cached, time.monotonic())
    return cached


def system_stats() -> dict[str, Any]:
    """Resumen de recursos locales para el HUD; no envía datos fuera del equipo."""
    memory: dict[str, float | None] = {"percent": None, "used_gb": None, "total_gb": None}
    if sys.platform == "win32":
        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            memory = {
                "percent": round(float(status.dwMemoryLoad), 1),
                "used_gb": round((status.ullTotalPhys - status.ullAvailPhys) / 1024 ** 3, 1),
                "total_gb": round(status.ullTotalPhys / 1024 ** 3, 1),
            }
    root = Path(PROJECT_ROOT.anchor or PROJECT_ROOT)
    usage = shutil.disk_usage(root)
    return {
        "cpu": {"percent": _cpu_percent()},
        "memory": memory,
        "disk": {"percent": round(usage.used / usage.total * 100, 1), "used_gb": round(usage.used / 1024 ** 3, 1), "total_gb": round(usage.total / 1024 ** 3, 1)},
        "gpu": {"percent": _gpu_percent()},
    }


def _powershell_json(script: str) -> Any:
    """Ejecuta una consulta local de Windows y devuelve su JSON sin usar shell."""
    if sys.platform != "win32":
        return None
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8, check=True,
        )
        return json.loads(completed.stdout) if completed.stdout.strip() else None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None


def get_pc_info() -> dict[str, Any]:
    """Inventario detallado del equipo, obtenido localmente con CIM de Windows."""
    if sys.platform != "win32":
        return {"error": "El inventario detallado está disponible únicamente en Windows.", "platform": sys.platform}
    script = r'''
      $os = Get-CimInstance Win32_OperatingSystem
      $cs = Get-CimInstance Win32_ComputerSystem
      [pscustomobject]@{
        cpu = @(Get-CimInstance Win32_Processor | ForEach-Object { [pscustomobject]@{ name=$_.Name; cores=$_.NumberOfCores; logical_processors=$_.NumberOfLogicalProcessors; max_mhz=$_.MaxClockSpeed } })
        gpu = @(Get-CimInstance Win32_VideoController | ForEach-Object { [pscustomobject]@{ name=$_.Name; driver=$_.DriverVersion; memory_gb=if ($_.AdapterRAM) {[math]::Round($_.AdapterRAM/1GB,1)} else {$null} } })
        memory = [pscustomobject]@{ total_gb=[math]::Round($cs.TotalPhysicalMemory/1GB,1); modules=@(Get-CimInstance Win32_PhysicalMemory | ForEach-Object { [pscustomobject]@{ capacity_gb=[math]::Round($_.Capacity/1GB,1); speed_mhz=$_.Speed; manufacturer=$_.Manufacturer; part_number=$_.PartNumber.Trim() } }) }
        storage = @(Get-CimInstance Win32_DiskDrive | ForEach-Object { [pscustomobject]@{ model=$_.Model; size_gb=[math]::Round($_.Size/1GB,1); interface=$_.InterfaceType; media_type=$_.MediaType } })
        motherboard = (Get-CimInstance Win32_BaseBoard | Select-Object -First 1 Manufacturer,Product,SerialNumber)
        bios = (Get-CimInstance Win32_BIOS | Select-Object -First 1 Manufacturer,SMBIOSBIOSVersion,ReleaseDate)
        windows = [pscustomobject]@{ caption=$os.Caption; version=$os.Version; build=$os.BuildNumber; architecture=$os.OSArchitecture }
        computer = [pscustomobject]@{ manufacturer=$cs.Manufacturer; model=$cs.Model; name=$env:COMPUTERNAME }
      } | ConvertTo-Json -Depth 6 -Compress
    '''
    data = _powershell_json(script)
    return data or {"error": "Windows no devolvió el inventario de hardware."}


def get_current_datetime() -> dict[str, str | int]:
    """Fecha y hora locales del equipo, sin depender de Internet."""
    now = datetime.now().astimezone()
    return {
        "iso": now.isoformat(timespec="seconds"), "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"), "weekday": now.strftime("%A"),
        "year": now.year, "timezone": str(now.tzinfo),
    }


def _read_memories() -> list[dict[str, str]]:
    try:
        stored = json.loads(MEMORIES_FILE.read_text(encoding="utf-8"))
        return stored if isinstance(stored, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _write_memories(memories: list[dict[str, str]]) -> None:
    MEMORIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = MEMORIES_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(memories, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(MEMORIES_FILE)


def remember_memory(content: str) -> dict[str, Any]:
    """Guarda una nota explícita del usuario en data/memories.json."""
    clean = content.strip()
    if not clean:
        return {"error": "La memoria no puede estar vacía."}
    if len(clean) > 2_000:
        return {"error": "La memoria no puede superar 2000 caracteres."}
    with _memories_lock:
        memories = _read_memories()
        if any(item.get("content", "").casefold() == clean.casefold() for item in memories):
            return {"ok": True, "duplicate": True, "memory": clean}
        entry = {"id": str(int(time.time() * 1000)), "content": clean, "created_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        memories.append(entry)
        _write_memories(memories)
    return {"ok": True, "memory": entry, "count": len(memories)}


def list_memories(query: str = "", max_results: int = 20) -> dict[str, Any]:
    """Recupera memorias guardadas; admite una búsqueda opcional."""
    needle = normalize(query) if query else ""
    with _memories_lock:
        memories = _read_memories()
    if needle:
        memories = [item for item in memories if needle in normalize(item.get("content", ""))]
    return {"memories": memories[-max(1, min(max_results, 100)):], "count": len(memories)}


def safe_path(relative_path: str) -> Path:
    """Restringe cualquier operación de archivos al workspace configurado."""
    candidate = (WORKSPACE / relative_path).resolve()
    try:
        candidate.relative_to(WORKSPACE)
    except ValueError as exc:
        raise ValueError("La ruta debe permanecer dentro del workspace autorizado.") from exc
    return candidate


def search_files(query: str, path: str = ".", max_results: int = 20) -> dict[str, Any]:
    base = safe_path(path)
    if not base.exists() or not base.is_dir():
        return {"error": "La carpeta indicada no existe."}

    needle = query.lower().strip()
    if not needle:
        return {"error": "Indica un texto o nombre que buscar."}
    results: list[dict[str, Any]] = []
    limit = max(1, min(max_results, 50))
    for file in base.rglob("*"):
        if any(part in IGNORED_DIRS for part in file.parts) or not file.is_file():
            continue
        try:
            relative = str(file.relative_to(WORKSPACE))
            name_match = needle in file.name.lower()
            content_match = False
            snippet = ""
            if file.stat().st_size <= 1_000_000:
                text = file.read_text(encoding="utf-8", errors="ignore")
                index = text.lower().find(needle)
                content_match = index >= 0
                if content_match:
                    snippet = text[max(0, index - 100): index + len(needle) + 180].replace("\n", " ")
            if name_match or content_match:
                results.append({"path": relative, "match": "nombre" if name_match else "contenido", "snippet": snippet})
                if len(results) >= limit:
                    break
        except (OSError, UnicodeError):
            continue
    return {"workspace": str(WORKSPACE), "results": results}


def create_file(path: str, content: str, overwrite: bool = False) -> dict[str, Any]:
    target = safe_path(path)
    if target.exists() and not overwrite:
        return {"error": "El archivo ya existe. Solicita overwrite=true solo si deseas reemplazarlo."}
    if target.exists() and target.is_dir():
        return {"error": "La ruta indicada es una carpeta, no un archivo."}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(target.relative_to(WORKSPACE)), "bytes": len(content.encode("utf-8"))}


def normalize(text: str) -> str:
    """Normaliza nombres para que configuración y configuracion coincidan."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return " ".join("".join(char for char in decomposed if not unicodedata.combining(char)).split())


def app_alias(app_name: str) -> list[str] | None:
    """Resuelve aliases comunes sin depender de cómo el modelo llame a la app."""
    requested = normalize(app_name)
    for alias, command in KNOWN_APPS.items():
        candidate = normalize(alias)
        if requested == candidate:
            return command
    return None


def _start_menu_apps() -> list[dict[str, str]]:
    """Obtiene accesos del usuario y del sistema sin invocar una shell."""
    roots = [
        Path(os.getenv("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        Path(os.getenv("PROGRAMDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    ]
    apps: list[dict[str, str]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for item in root.rglob("*"):
            if item.is_file() and item.suffix.lower() in {".lnk", ".url", ".exe"}:
                apps.append({"name": item.stem, "path": str(item), "source": "menu_inicio"})
    return apps


def _registered_apps() -> list[dict[str, str]]:
    """Lee App Paths de Windows; sirve para apps que no publican acceso directo."""
    if sys.platform != "win32":
        return []
    import winreg

    apps: list[dict[str, str]] = []
    locations = [
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths"),
    ]
    for hive, location in locations:
        try:
            with winreg.OpenKey(hive, location) as key:
                index = 0
                while True:
                    try:
                        child_name = winreg.EnumKey(key, index)
                        index += 1
                        with winreg.OpenKey(key, child_name) as child:
                            executable, _ = winreg.QueryValueEx(child, None)
                        if executable:
                            apps.append({"name": Path(child_name).stem, "path": str(executable), "source": "registro"})
                    except OSError:
                        break
        except OSError:
            continue
    return apps


def search_apps(query: str, max_results: int = 12) -> dict[str, Any]:
    """Busca aplicaciones instaladas por nombre dentro de fuentes locales de Windows."""
    needle = normalize(query)
    if not needle:
        return {"error": "Indica el nombre de una aplicación."}
    apps = [{"name": name.title(), "path": " ".join(command), "source": "sistema"} for name, command in KNOWN_APPS.items()]
    apps.extend(_start_menu_apps())
    apps.extend(_registered_apps())
    seen: set[str] = set()
    results: list[dict[str, str]] = []
    for app in sorted(apps, key=lambda item: (normalize(item["name"]) != needle, not normalize(item["name"]).startswith(needle), normalize(item["name"]))):
        if needle not in normalize(app["name"]) and needle not in normalize(Path(app["path"]).name):
            continue
        key = app["path"].lower()
        if key not in seen:
            seen.add(key)
            results.append(app)
        if len(results) >= max(1, min(max_results, 30)):
            break
    return {"results": results, "count": len(results)}


def _visible_window_pids() -> set[int]:
    """Devuelve procesos que ya tienen ventanas visibles (Windows solamente)."""
    if sys.platform != "win32":
        return set()
    user32 = ctypes.windll.user32
    pids: set[int] = set()
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def visit(hwnd: int, _: int) -> bool:
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd):
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                pids.add(pid.value)
        return True

    user32.EnumWindows(callback_type(visit), 0)
    return pids


def _focus_new_window(existing_pids: set[int], preferred_pid: int | None = None) -> None:
    """Intenta restaurar y traer al frente la ventana recién abierta.

    Windows puede negar SetForegroundWindow por sus reglas de foco; el fallo es
    inocuo y la aplicación igualmente se abre.
    """
    if sys.platform != "win32":
        return
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    candidate = ctypes.c_void_p()

    def visit(hwnd: int, _: int) -> bool:
        if not user32.IsWindowVisible(hwnd) or not user32.GetWindowTextLengthW(hwnd):
            return True
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        # Las UWP suelen crear una ventana en ApplicationFrameHost, cuyo PID
        # difiere del lanzador; por eso también aceptamos cualquier ventana nueva.
        if pid.value == preferred_pid or pid.value not in existing_pids:
            candidate.value = hwnd
            return False
        return True

    # Algunas apps UWP (p. ej. Calculadora) tardan en crear la ventana.
    for _ in range(8):
        candidate.value = None
        user32.EnumWindows(callback_type(visit), 0)
        if candidate.value:
            user32.ShowWindow(candidate, 9)  # SW_RESTORE
            user32.SetForegroundWindow(candidate)
            return
        time.sleep(0.15)


def _request_foreground(existing_pids: set[int], preferred_pid: int | None = None) -> None:
    threading.Thread(target=_focus_new_window, args=(existing_pids, preferred_pid), daemon=True).start()


def open_app(app_name: str) -> dict[str, Any]:
    if not ENABLE_APP_LAUNCH:
        return {"error": "Abrir aplicaciones está desactivado con ENABLE_APP_LAUNCH=false."}
    command = app_alias(app_name)
    existing_pids = _visible_window_pids()
    try:
        if command:
            if command[0].endswith(":"):
                os.startfile(command[0])
                _request_foreground(existing_pids)
            else:
                process = subprocess.Popen(command, cwd=WORKSPACE)
                _request_foreground(existing_pids, process.pid)
            return {"ok": True, "opened": app_name, "source": "sistema", "foreground_requested": True}

        found = search_apps(app_name, max_results=1).get("results", [])
        if not found:
            return {"error": f"No encontré una aplicación llamada '{app_name}'. Usa search_apps para ver coincidencias."}
        app = found[0]
        # startfile abre .lnk, .url o .exe mediante la asociación de Windows, sin shell.
        os.startfile(app["path"])
        _request_foreground(existing_pids)
        return {"ok": True, "opened": app["name"], "source": app["source"], "path": app["path"], "foreground_requested": True}
    except OSError as exc:
        return {"error": f"No se pudo abrir la aplicación: {exc}"}


def list_running_apps(max_results: int = 40) -> dict[str, Any]:
    """Lista los procesos activos para que el asistente pueda orientarse."""
    if sys.platform != "win32":
        return {"error": "Esta herramienta solo está disponible en Windows."}
    limit = max(1, min(max_results, 100))
    try:
        completed = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        return {"error": f"No se pudo consultar los procesos: {exc}"}
    names: list[str] = []
    seen: set[str] = set()
    for line in completed.stdout.splitlines():
        match = re.match(r'^"([^"]+)"', line)
        if match and match.group(1).casefold() not in seen:
            seen.add(match.group(1).casefold())
            names.append(match.group(1))
        if len(names) >= limit:
            break
    return {"processes": names, "count": len(names)}


def screen_size() -> dict[str, int] | dict[str, str]:
    """Devuelve el tamaño del escritorio para usar coordenadas de ratón."""
    if sys.platform != "win32":
        return {"error": "Esta herramienta solo está disponible en Windows."}
    user32 = __import__("ctypes").windll.user32
    return {"width": user32.GetSystemMetrics(0), "height": user32.GetSystemMetrics(1)}


def move_mouse(x: int, y: int) -> dict[str, Any]:
    """Mueve el cursor a una coordenada absoluta del escritorio Windows."""
    size = screen_size()
    if "error" in size:
        return size
    if not 0 <= x < size["width"] or not 0 <= y < size["height"]:
        return {"error": f"Coordenadas fuera de pantalla: {x}, {y}. Tamaño: {size['width']}x{size['height']}."}
    user32 = __import__("ctypes").windll.user32
    if not user32.SetCursorPos(x, y):
        return {"error": "Windows no permitió mover el cursor."}
    return {"ok": True, "position": {"x": x, "y": y}}


def click_mouse(x: int, y: int, button: str = "left", clicks: int = 1) -> dict[str, Any]:
    """Mueve y hace clic; solo se usa tras una orden explícita del usuario."""
    moved = move_mouse(x, y)
    if not moved.get("ok"):
        return moved
    if button not in {"left", "right"}:
        return {"error": "El botón debe ser left o right."}
    count = max(1, min(clicks, 3))
    user32 = __import__("ctypes").windll.user32
    down, up = (0x0002, 0x0004) if button == "left" else (0x0008, 0x0010)
    for _ in range(count):
        user32.mouse_event(down, 0, 0, 0, 0)
        user32.mouse_event(up, 0, 0, 0, 0)
    return {"ok": True, "position": {"x": x, "y": y}, "button": button, "clicks": count}


def scroll_mouse(amount: int) -> dict[str, Any]:
    """Desplaza la ventana situada bajo el cursor. Positivo=sube, negativo=baja."""
    if sys.platform != "win32":
        return {"error": "Esta herramienta solo está disponible en Windows."}
    delta = max(-20, min(amount, 20))
    if not delta:
        return {"error": "Indica un desplazamiento diferente de cero."}
    __import__("ctypes").windll.user32.mouse_event(0x0800, 0, 0, delta * 120, 0)
    return {"ok": True, "amount": delta}


def open_browser(urls: list[str], browser: str = "chrome") -> dict[str, Any]:
    """Abre URLs en Chrome o, si se pide, en el navegador predeterminado."""
    cleaned = [url for url in urls if isinstance(url, str) and url.startswith(("http://", "https://"))]
    if not cleaned:
        return {"error": "Indica al menos una URL http o https válida."}
    cleaned = cleaned[:10]
    browser_name = normalize(browser)
    existing_pids = _visible_window_pids()
    try:
        if browser_name in {"default", "predeterminado"}:
            for url in cleaned:
                os.startfile(url)
            _request_foreground(existing_pids)
            return {"ok": True, "browser": "predeterminado", "urls": cleaned, "foreground_requested": True}
        found = search_apps(browser_name, max_results=12).get("results", [])
        executable = next((item["path"] for item in found if item["path"].lower().endswith(".exe")), None)
        if not executable:
            return {"error": f"No encontré el ejecutable de {browser}. Usa search_apps para comprobar la instalación."}
        process = subprocess.Popen([executable, *cleaned], cwd=WORKSPACE)
        _request_foreground(existing_pids, process.pid)
        return {"ok": True, "browser": browser, "urls": cleaned, "path": executable, "foreground_requested": True}
    except OSError as exc:
        return {"error": f"No se pudo abrir el navegador: {exc}"}


TOOLS = [
    {"type": "function", "function": {"name": "search_files", "description": "Busca nombres o contenido de archivos dentro del workspace autorizado.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string", "default": "."}, "max_results": {"type": "integer", "default": 20}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "create_file", "description": "Crea un archivo UTF-8 dentro del workspace. No reemplaza archivos existentes salvo que se indique overwrite.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "overwrite": {"type": "boolean", "default": False}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "get_pc_info", "description": "Obtiene componentes y datos del PC local: CPU, GPU, RAM, discos, placa madre, BIOS y versión de Windows. Úsala ante preguntas sobre el equipo del usuario.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_current_datetime", "description": "Obtiene fecha, hora, día, año y zona horaria locales del equipo. Úsala para cualquier pregunta temporal actual.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "remember_memory", "description": "Guarda una memoria que el usuario pidió explícitamente recordar. Conserva literalmente el dato importante, incluidas fechas personales.", "parameters": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}}},
    {"type": "function", "function": {"name": "list_memories", "description": "Consulta las memorias persistentes que el usuario pidió guardar. Úsala ante 'qué recuerdas', cumpleaños, recordatorios o datos personales guardados.", "parameters": {"type": "object", "properties": {"query": {"type": "string", "default": ""}, "max_results": {"type": "integer", "default": 20}}}}},
    {"type": "function", "function": {"name": "search_apps", "description": "Busca aplicaciones instaladas en el menú Inicio y registro local de Windows. Úsala para localizar una aplicación antes de abrirla si el nombre es ambiguo.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer", "default": 12}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "open_app", "description": "Abre una aplicación local por nombre. Úsala solo cuando el usuario solicite claramente abrirla. Para nombres ambiguos, busca primero con search_apps.", "parameters": {"type": "object", "properties": {"app_name": {"type": "string"}}, "required": ["app_name"]}}},
    {"type": "function", "function": {"name": "list_running_apps", "description": "Lista procesos actualmente en ejecución en Windows. Úsala cuando el usuario pregunte qué aplicaciones están abiertas.", "parameters": {"type": "object", "properties": {"max_results": {"type": "integer", "default": 40}}}}},
    {"type": "function", "function": {"name": "screen_size", "description": "Obtiene el ancho y alto del escritorio Windows antes de usar coordenadas de ratón.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "move_mouse", "description": "Mueve el cursor a coordenadas absolutas. Úsala únicamente cuando el usuario indique coordenadas explícitas.", "parameters": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}}, "required": ["x", "y"]}}},
    {"type": "function", "function": {"name": "click_mouse", "description": "Mueve el cursor y hace clic en coordenadas absolutas. Úsala únicamente con una orden explícita del usuario.", "parameters": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "button": {"type": "string", "enum": ["left", "right"], "default": "left"}, "clicks": {"type": "integer", "default": 1}}, "required": ["x", "y"]}}},
    {"type": "function", "function": {"name": "scroll_mouse", "description": "Desplaza bajo el cursor. amount positivo sube y negativo baja. Úsala solo cuando el usuario lo solicite claramente.", "parameters": {"type": "object", "properties": {"amount": {"type": "integer"}}, "required": ["amount"]}}},
    {"type": "function", "function": {"name": "open_browser", "description": "Abre hasta diez URLs en Chrome o en el navegador predeterminado. Úsala al recibir una orden explícita de navegar o abrir sitios web.", "parameters": {"type": "object", "properties": {"urls": {"type": "array", "items": {"type": "string"}}, "browser": {"type": "string", "default": "chrome"}}, "required": ["urls"]}}},
]
TOOL_HANDLERS = {"search_files": search_files, "create_file": create_file, "get_pc_info": get_pc_info, "get_current_datetime": get_current_datetime, "remember_memory": remember_memory, "list_memories": list_memories, "search_apps": search_apps, "open_app": open_app, "list_running_apps": list_running_apps, "screen_size": screen_size, "move_mouse": move_mouse, "click_mouse": click_mouse, "scroll_mouse": scroll_mouse, "open_browser": open_browser}


def textual_tool_call(content: str) -> dict[str, Any] | None:
    """Compatibilidad para modelos que escriben `open_app "X"` en vez del JSON nativo.

    Algunos modelos locales conocen la herramienta, pero no respetan el formato
    tool_call de Ollama. Solo acepta una llamada aislada a las herramientas de
    apps; nunca interpreta ni ejecuta comandos de shell.
    """
    clean = content.strip().strip("`")
    simple = re.fullmatch(r"(get_pc_info|get_current_datetime|list_memories)\s*\(\s*\)", clean, flags=re.IGNORECASE)
    if simple:
        return {"function": {"name": simple.group(1).lower(), "arguments": {}}}
    match = re.fullmatch(r"(open_app|search_apps)\s*(?:\(\s*)?(.+?)\s*\)?", clean, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    name, argument = match.groups()
    argument = argument.strip().strip("()[]{}'\" ")
    if not argument:
        return None
    return {"function": {"name": name.lower(), "arguments": {"app_name" if name.lower() == "open_app" else "query": argument}}}


def explicit_app_request(message: str) -> dict[str, Any] | None:
    """Ejecuta órdenes explícitas de abrir apps aunque el modelo omita tool_calls."""
    match = re.search(
        r"\b(?:abre|abrir|inicia|iniciar|ejecuta|ejecutar|lanza|lanzar)\s+(?:la |el |un |una )?(.+?)(?:[.!?]|$)",
        message,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    app_name = match.group(1).strip(" .,!¿?\"'")
    # No convertir frases largas o compuestas en nombres de ejecutable.
    if not app_name or len(app_name) > 80:
        return None
    return {"function": {"name": "open_app", "arguments": {"app_name": app_name}}}


def explicit_memory_request(message: str) -> dict[str, Any] | None:
    """Guarda o consulta memorias incluso con modelos que no emiten tool_calls."""
    clean = message.strip()
    save = re.search(r"\b(?:recuerda|recordar|guarda|guardar|anota|anotar)\s+(?:que\s+)?(.+)", clean, flags=re.IGNORECASE | re.DOTALL)
    if save:
        content = save.group(1).strip(" .!¿?\"'")
        if content:
            return {"function": {"name": "remember_memory", "arguments": {"content": content}}}
    if re.search(r"\b(?:que|qué)\s+recuerdas\b|\bmis\s+(?:memorias|recordatorios)\b", clean, flags=re.IGNORECASE):
        return {"function": {"name": "list_memories", "arguments": {}}}
    return None


def explicit_pc_info_request(message: str) -> dict[str, Any] | None:
    clean = normalize(message)
    triggers = ("que componentes", "componentes tengo", "mi pc", "mi computador", "mi equipo", "hardware", "que cpu", "que gpu", "cuanta ram", "placa madre", "version de windows", "almacenamiento tengo")
    if any(trigger in clean for trigger in triggers):
        return {"function": {"name": "get_pc_info", "arguments": {}}}
    return None


def explicit_datetime_request(message: str) -> dict[str, Any] | None:
    clean = normalize(message)
    if any(trigger in clean for trigger in ("hora", "fecha", " dia", " ano", "year")):
        return {"function": {"name": "get_current_datetime", "arguments": {}}}
    return None


def explicit_app_search(message: str) -> dict[str, Any] | None:
    """Hace verificable una petición explícita de buscar aplicaciones."""
    match = re.search(
        r"\b(?:busca|buscar|encuentra|encontrar)\s+(?:aplicaciones?|apps?)?\s*(?:llamadas?|con el nombre de)?\s*[\"“]?(.+?)[\"”]?(?:[.!?]|$)",
        message,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    query = match.group(1).strip(" .,!¿?\"'“”")
    if not query or len(query) > 80:
        return None
    return {"function": {"name": "search_apps", "arguments": {"query": query}}}


def explicit_browser_request(message: str) -> dict[str, Any] | None:
    """Resuelve órdenes como 'abre Chrome con YouTube, Gmail y busca X'."""
    match = re.search(
        r"\b(?:abre|abrir|inicia|iniciar)\s+(?:google\s+)?chrome\b(?:\s+(?:con|y)\s+(.+))?",
        message,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    details = normalize(match.group(1) or "")
    urls: list[str] = []
    if "youtube" in details:
        urls.append("https://www.youtube.com/")
    if "gmail" in details or "correo" in details:
        urls.append("https://mail.google.com/")
    search = re.search(r"\b(?:y\s+)?busca\s+(.+?)(?:[.!?]|$)", match.group(1) or "", flags=re.IGNORECASE)
    if search and search.group(1).strip():
        urls.append("https://www.google.com/search?q=" + quote_plus(search.group(1).strip()))
    if not urls:
        urls = ["https://www.google.com/"]
    return {"function": {"name": "open_browser", "arguments": {"browser": "chrome", "urls": urls}}}


def explicit_mouse_request(message: str) -> dict[str, Any] | None:
    """Reconoce acciones de ratón solo si contienen coordenadas explícitas."""
    clean = normalize(message)
    click = re.search(r"\b(?:haz|hacer|da|dar)\s+(?:un\s+)?(doble\s+)?clic(?:\s+derecho)?\s+(?:en\s+)?(\d+)\s*[,x]\s*(\d+)", clean, flags=re.IGNORECASE)
    if click:
        right = "clic derecho" in clean
        return {"function": {"name": "click_mouse", "arguments": {"x": int(click.group(2)), "y": int(click.group(3)), "button": "right" if right else "left", "clicks": 2 if click.group(1) else 1}}}
    move = re.search(r"\b(?:mueve|mover|lleva|llevar)\s+(?:el\s+)?raton\s+(?:a|hasta)\s*(\d+)\s*[,x]\s*(\d+)", clean, flags=re.IGNORECASE)
    if move:
        return {"function": {"name": "move_mouse", "arguments": {"x": int(move.group(1)), "y": int(move.group(2))}}}
    scroll = re.search(r"\b(?:desplaza|desplazar|scroll)\s+(?:hacia\s+)?(arriba|abajo)\b", clean, flags=re.IGNORECASE)
    if scroll:
        return {"function": {"name": "scroll_mouse", "arguments": {"amount": 5 if scroll.group(1) == "arriba" else -5}}}
    return None


def sse(data: str) -> str:
    return "data: " + data.replace("\n", "\\n") + "\n\n"


async def ollama_chat(client: httpx.AsyncClient, messages: list[dict[str, Any]], stream: bool) -> httpx.Response:
    response = await client.post(
        f"{OLLAMA_URL}/api/chat",
        json={"model": OLLAMA_MODEL, "messages": messages, "tools": TOOLS, "stream": stream},
    )
    response.raise_for_status()
    return response


async def agent_stream(request: ChatRequest) -> AsyncIterator[str]:
    history = [item for item in request.history[-MAX_HISTORY:] if item.get("role") in {"user", "assistant"} and item.get("content")]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "Eres Sellium, un asistente local. Usa herramientas cuando aporten valor y ejecútalas en vez de afirmar que lo harás. Puedes buscar y abrir aplicaciones o páginas web solo cuando el usuario lo solicite claramente. Las acciones de ratón solo se realizan con una orden explícita y coordenadas. Usa search_apps si el nombre de una aplicación es ambiguo. Las herramientas de archivos nunca pueden salir del workspace. Antes de reemplazar un archivo existente, confirma la intención del usuario."},
        *history,
        {"role": "user", "content": request.message},
    ]
    direct_call = (
        explicit_memory_request(request.message)
        or explicit_pc_info_request(request.message)
        or explicit_datetime_request(request.message)
        or explicit_browser_request(request.message)
        or explicit_mouse_request(request.message)
        or explicit_app_request(request.message)
        or explicit_app_search(request.message)
    )
    if direct_call:
        # La orden proviene del usuario actual, no de una respuesta generada. Así
        # funciona con modelos que contestan en prosa en lugar del JSON de Ollama.
        messages.append({"role": "assistant", "content": "", "tool_calls": [direct_call]})
        function = direct_call["function"]
        result = TOOL_HANDLERS[function["name"]](**function["arguments"])
        messages.append({"role": "tool", "tool_name": function["name"], "content": json.dumps(result, ensure_ascii=False)})
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=5.0)) as client:
            for _ in range(6):  # evita ciclos infinitos de llamadas a herramientas
                response = await ollama_chat(client, messages, stream=False)
                assistant_message = response.json().get("message", {})
                calls = assistant_message.get("tool_calls") or []
                fallback = textual_tool_call(assistant_message.get("content", ""))
                if not calls and fallback:
                    calls = [fallback]
                    assistant_message["tool_calls"] = calls
                    assistant_message["content"] = ""
                messages.append(assistant_message)
                if not calls:
                    # Se solicita de nuevo con streaming para entregar el texto al frontend gradualmente.
                    async with client.stream("POST", f"{OLLAMA_URL}/api/chat", json={"model": OLLAMA_MODEL, "messages": messages[:-1], "stream": True}) as stream_response:
                        stream_response.raise_for_status()
                        async for line in stream_response.aiter_lines():
                            if line:
                                chunk = json.loads(line)
                                content = chunk.get("message", {}).get("content", "")
                                if content:
                                    yield sse(content)
                    yield "data: [DONE]\n\n"
                    return
                for call in calls:
                    function = call.get("function", {})
                    name, arguments = function.get("name"), function.get("arguments", {})
                    try:
                        result = TOOL_HANDLERS[name](**arguments) if name in TOOL_HANDLERS else {"error": "Herramienta no disponible."}
                    except (TypeError, ValueError, OSError) as exc:
                        result = {"error": str(exc)}
                    messages.append({"role": "tool", "tool_name": name, "content": json.dumps(result, ensure_ascii=False)})
            yield sse("No pude completar la tarea porque se alcanzó el límite de pasos de herramientas.")
            yield "data: [DONE]\n\n"
    except httpx.HTTPError as exc:
        yield sse(f"Error al conectar con Ollama: {exc}")
        yield "data: [DONE]\n\n"


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "model": OLLAMA_MODEL, "workspace": str(WORKSPACE), "app_launch_enabled": ENABLE_APP_LAUNCH}


@app.get("/api/system/stats")
async def get_system_stats() -> dict[str, Any]:
    """Métricas para el dashboard local de la interfaz."""
    return await asyncio.to_thread(system_stats)


@app.post("/api/chat")
async def chat(request: ChatRequest) -> StreamingResponse:
    return StreamingResponse(agent_stream(request), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.post("/api/stt")
async def stt(file: UploadFile = File(...)) -> dict[str, str]:
    audio = await file.read()
    if len(audio) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="El audio no puede superar 20 MB.")
    suffix = Path(file.filename or "recording.webm").suffix or ".webm"
    try:
        return {"text": await asyncio.to_thread(transcribe, audio, suffix)}
    except VoiceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/tts")
async def tts(request: TTSRequest) -> Response:
    try:
        audio = await asyncio.to_thread(synthesize_wav, request.text)
    except VoiceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return Response(content=audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})
