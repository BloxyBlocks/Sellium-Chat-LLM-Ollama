"""Descarga única de modelos. En ejecución normal no se requiere Internet."""
from __future__ import annotations

import shutil
import urllib.request
from pathlib import Path

from huggingface_hub import snapshot_download


ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
PIPER_DIR = MODELS / "piper"
PIPER_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_ES/davefx/medium/"
PIPER_FILES = ("es_ES-davefx-medium.onnx", "es_ES-davefx-medium.onnx.json")


def download(url: str, destination: Path) -> None:
    if destination.exists():
        print(f"Ya existe: {destination.name}")
        return
    print(f"Descargando: {destination.name}")
    urllib.request.urlretrieve(url, destination)


def main() -> None:
    PIPER_DIR.mkdir(parents=True, exist_ok=True)
    for name in PIPER_FILES:
        download(PIPER_BASE + name, PIPER_DIR / name)

    whisper_dir = MODELS / "faster-whisper-small"
    if whisper_dir.exists():
        print("Ya existe: Faster-Whisper small")
    else:
        print("Descargando Faster-Whisper small (solo esta vez)...")
        downloaded = Path(snapshot_download("Systran/faster-whisper-small", local_dir=str(whisper_dir)))
        if downloaded != whisper_dir:
            shutil.copytree(downloaded, whisper_dir, dirs_exist_ok=True)
    print("Modelos listos. El backend puede ejecutarse sin Internet.")


if __name__ == "__main__":
    main()
