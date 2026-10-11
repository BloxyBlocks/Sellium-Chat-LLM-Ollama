# Sellium: backend local con Ollama

El frontend ya apunta a `http://localhost:8000/api/chat`. Este backend transmite las respuestas de Ollama por SSE y permite al modelo utilizar herramientas locales controladas.

## Interfaz y arranque cinematográfico

El chat original se ha restaurado e integrado con el boot WebGL actual. La pantalla de inicio usa Three.js y GSAP; al terminar revela la interfaz completa sin modificar sus rutas locales ni el protocolo de streaming.

- `index.html` reúne el marcado del boot y del chat, además de su lógica de interacción.
- `style.css` separa los estilos del boot y de la interfaz del chat.
- `boot.js` contiene exclusivamente la secuencia WebGL reutilizable.

Se mantienen el chat SSE con Ollama, historial de conversación, renderizado de Markdown/código con botón de copiar, visualizador interactivo, micrófono/STT y voz TTS. El inicio requiere cinco toques: cada uno reproduce `Assets/Sound/golpe.mp3` y amplía la animación; el último reproduce `Assets/Sound/run2.mp3` y da paso al boot WebGL. Al finalizar, el orbe aparece con el parpadeo y sonido originales. Estos valores se pueden ajustar en `CFG` dentro de `index.html` (`TAP_SOUND`, `TAP_VOLUME`, `BOOT_SOUND`, `BOOT_VOLUME`, `BLINK_SOUND` y `BLINK_VOLUME`). Durante la aplicación, presiona `R` para volver a ejecutar el arranque (cuando no haya una respuesta en curso).

## Inicio

En PowerShell, desde la raíz del proyecto:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python scripts/download_voice_models.py
uvicorn backend.main:app --reload --port 8000
```

Ollama debe estar ejecutándose y tener el modelo configurado. El predeterminado es `qwen2.5-coder:7b`, que ya puedes verificar con `ollama list`.

Variables opcionales (puedes establecerlas antes de iniciar Uvicorn):

```powershell
$env:OLLAMA_MODEL = 'qwen2.5-coder:7b'
$env:ENABLE_APP_LAUNCH = 'true' # activa explorer, notepad, calculator y vscode
uvicorn backend.main:app --reload --port 8000
```

## Control local de aplicaciones

Con `ENABLE_APP_LAUNCH=true`, Sellium puede buscar aplicaciones instaladas en el menú Inicio y el registro de Windows, abrirlas y consultar los procesos activos. Las órdenes explícitas de abrir o buscar una app se ejecutan directamente por el backend, sin depender de que el modelo local emita correctamente un `tool_call`.

Ejemplos:

```text
Abre la calculadora
Inicia Visual Studio Code
Busca aplicaciones llamadas Chrome; no abras ninguna
Qué aplicaciones están abiertas
```

La aplicación de escritorio y el navegador requieren que FastAPI siga activo en `http://127.0.0.1:8000`. Si Sellium muestra una respuesta de demostración, el backend no está disponible; inicia Uvicorn con el comando de la sección anterior.

## Límites de seguridad del prototipo

- Las operaciones de archivo están confinadas a `AGENT_WORKSPACE` (por defecto, la raíz de este proyecto). No puede acceder a rutas externas mediante `..`.
- `create_file` no reemplaza un archivo a menos que la herramienta reciba `overwrite=true`; el asistente está instruido para pedir confirmación antes de ello.
- Abrir aplicaciones está activo por defecto para este asistente local y se puede desactivar con `ENABLE_APP_LAUNCH=false`. Solo abre aliases conocidos o accesos descubiertos en el menú Inicio/registro; no existe ejecución arbitraria de comandos.
- No expongas este servidor a la red: está pensado exclusivamente para `localhost` durante el prototipo.

## Voz local (Fase 2)

El botón de micrófono del frontend ahora graba con `MediaRecorder`, envía el audio a `POST /api/stt` y Faster-Whisper lo transcribe localmente. Al finalizar una respuesta, el navegador pide `POST /api/tts` y reproduce el WAV generado localmente por Piper.

`python scripts/download_voice_models.py` necesita Internet **una única vez** para guardar los modelos en `models/`:

- Faster-Whisper `small` para español.
- Piper `es_ES-davefx-medium`, voz masculina de español de España y un solo locutor. La síntesis predeterminada usa velocidad `1.1x` (`PIPER_SPEED=1.1`) y ruido `0.5` (`PIPER_NOISE_SCALE=0.5`).

En ejecuciones posteriores, el backend solo abre esos archivos locales; no transmite audio ni texto a servicios externos. La primera petición calienta los modelos y será más lenta. La meta de ~200 ms se aplica a la síntesis Piper ya cargada y a frases cortas, pero no puede garantizarse en todos los equipos ni incluye el tiempo de generación de Ollama o de la red local.

## Aplicación nativa de Windows (Tauri v2)

El proyecto incluye un contenedor Tauri v2 que empaqueta `index.html` y `Assets/` como aplicación Windows. Para construirlo:

```powershell
npm install
npm run build
```

El instalador NSIS se crea en `src-tauri/target/release/bundle/nsis/`. El bundle incluye los archivos `es_ES-davefx-medium.onnx` y `es_ES-davefx-medium.onnx.json` como recursos; antes de compilar, ejecuta `python scripts/download_voice_models.py` para que estén disponibles. Esta primera versión nativa consume el backend local existente en `http://localhost:8000`; inicia FastAPI/Ollama antes de abrir la aplicación. La incorporación de Python, modelos de Whisper/Piper y Ollama como *sidecars* autocontenidos se realizará como una fase de empaquetado separada, porque añade varios GB al instalador.
