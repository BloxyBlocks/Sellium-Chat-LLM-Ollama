"""Backend local de Sellium: Ollama + herramientas controladas del equipo."""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
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
    # Navegador normal y WebView de Tauri v2. Sin estos orígenes el preflight
    # OPTIONS del cliente nativo se rechaza antes de llegar a /api/chat o /api/tts.
    allow_origins=[
        "http://localhost",
        "http://127.0.0.1",
        "http://tauri.localhost",
        "tauri://localhost",
        "null",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12_000)
    history: list[dict[str, str]] = Field(default_factory=list)


class TTSRequest(BaseModel):
    text: str = Field(min_length=1, max_length=6_000)


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


def open_app(app_name: str) -> dict[str, Any]:
    if not ENABLE_APP_LAUNCH:
        return {"error": "Abrir aplicaciones está desactivado con ENABLE_APP_LAUNCH=false."}
    command = app_alias(app_name)
    try:
        if command:
            if command[0].endswith(":"):
                os.startfile(command[0])
            else:
                subprocess.Popen(command, cwd=WORKSPACE)
            return {"ok": True, "opened": app_name, "source": "sistema"}

        found = search_apps(app_name, max_results=1).get("results", [])
        if not found:
            return {"error": f"No encontré una aplicación llamada '{app_name}'. Usa search_apps para ver coincidencias."}
        app = found[0]
        # startfile abre .lnk, .url o .exe mediante la asociación de Windows, sin shell.
        os.startfile(app["path"])
        return {"ok": True, "opened": app["name"], "source": app["source"], "path": app["path"]}
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
    try:
        if browser_name in {"default", "predeterminado"}:
            for url in cleaned:
                os.startfile(url)
            return {"ok": True, "browser": "predeterminado", "urls": cleaned}
        found = search_apps(browser_name, max_results=12).get("results", [])
        executable = next((item["path"] for item in found if item["path"].lower().endswith(".exe")), None)
        if not executable:
            return {"error": f"No encontré el ejecutable de {browser}. Usa search_apps para comprobar la instalación."}
        subprocess.Popen([executable, *cleaned], cwd=WORKSPACE)
        return {"ok": True, "browser": browser, "urls": cleaned, "path": executable}
    except OSError as exc:
        return {"error": f"No se pudo abrir el navegador: {exc}"}


TOOLS = [
    {"type": "function", "function": {"name": "search_files", "description": "Busca nombres o contenido de archivos dentro del workspace autorizado.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "path": {"type": "string", "default": "."}, "max_results": {"type": "integer", "default": 20}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "create_file", "description": "Crea un archivo UTF-8 dentro del workspace. No reemplaza archivos existentes salvo que se indique overwrite.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "overwrite": {"type": "boolean", "default": False}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "search_apps", "description": "Busca aplicaciones instaladas en el menú Inicio y registro local de Windows. Úsala para localizar una aplicación antes de abrirla si el nombre es ambiguo.", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer", "default": 12}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "open_app", "description": "Abre una aplicación local por nombre. Úsala solo cuando el usuario solicite claramente abrirla. Para nombres ambiguos, busca primero con search_apps.", "parameters": {"type": "object", "properties": {"app_name": {"type": "string"}}, "required": ["app_name"]}}},
    {"type": "function", "function": {"name": "list_running_apps", "description": "Lista procesos actualmente en ejecución en Windows. Úsala cuando el usuario pregunte qué aplicaciones están abiertas.", "parameters": {"type": "object", "properties": {"max_results": {"type": "integer", "default": 40}}}}},
    {"type": "function", "function": {"name": "screen_size", "description": "Obtiene el ancho y alto del escritorio Windows antes de usar coordenadas de ratón.", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "move_mouse", "description": "Mueve el cursor a coordenadas absolutas. Úsala únicamente cuando el usuario indique coordenadas explícitas.", "parameters": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}}, "required": ["x", "y"]}}},
    {"type": "function", "function": {"name": "click_mouse", "description": "Mueve el cursor y hace clic en coordenadas absolutas. Úsala únicamente con una orden explícita del usuario.", "parameters": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "button": {"type": "string", "enum": ["left", "right"], "default": "left"}, "clicks": {"type": "integer", "default": 1}}, "required": ["x", "y"]}}},
    {"type": "function", "function": {"name": "scroll_mouse", "description": "Desplaza bajo el cursor. amount positivo sube y negativo baja. Úsala solo cuando el usuario lo solicite claramente.", "parameters": {"type": "object", "properties": {"amount": {"type": "integer"}}, "required": ["amount"]}}},
    {"type": "function", "function": {"name": "open_browser", "description": "Abre hasta diez URLs en Chrome o en el navegador predeterminado. Úsala al recibir una orden explícita de navegar o abrir sitios web.", "parameters": {"type": "object", "properties": {"urls": {"type": "array", "items": {"type": "string"}}, "browser": {"type": "string", "default": "chrome"}}, "required": ["urls"]}}},
]
TOOL_HANDLERS = {"search_files": search_files, "create_file": create_file, "search_apps": search_apps, "open_app": open_app, "list_running_apps": list_running_apps, "screen_size": screen_size, "move_mouse": move_mouse, "click_mouse": click_mouse, "scroll_mouse": scroll_mouse, "open_browser": open_browser}


def textual_tool_call(content: str) -> dict[str, Any] | None:
    """Compatibilidad para modelos que escriben `open_app "X"` en vez del JSON nativo.

    Algunos modelos locales conocen la herramienta, pero no respetan el formato
    tool_call de Ollama. Solo acepta una llamada aislada a las herramientas de
    apps; nunca interpreta ni ejecuta comandos de shell.
    """
    clean = content.strip().strip("`")
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
        explicit_browser_request(request.message)
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
