"""Servicios de voz completamente locales una vez instalados los modelos."""
from __future__ import annotations

import io
import os
import tempfile
import wave
from functools import lru_cache
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
WHISPER_MODEL = Path(os.getenv("WHISPER_MODEL_PATH", PROJECT_ROOT / "models" / "faster-whisper-small"))
PIPER_MODEL = Path(os.getenv("PIPER_MODEL_PATH", PROJECT_ROOT / "models" / "piper" / "es_ES-davefx-medium.onnx"))
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
# Davefx es una voz de un solo locutor; se conserva el ID por compatibilidad.
PIPER_SPEAKER_ID = int(os.getenv("PIPER_SPEAKER_ID", "0"))
PIPER_SPEED = float(os.getenv("PIPER_SPEED", "1.1"))
PIPER_NOISE_SCALE = float(os.getenv("PIPER_NOISE_SCALE", "0.5"))


class VoiceUnavailable(RuntimeError):
    pass


@lru_cache(maxsize=1)
def whisper_model():
    if not WHISPER_MODEL.is_dir():
        raise VoiceUnavailable(f"Falta el modelo Faster-Whisper local: {WHISPER_MODEL}")
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise VoiceUnavailable("Falta faster-whisper. Instala requirements.txt.") from exc
    return WhisperModel(str(WHISPER_MODEL), device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE)


@lru_cache(maxsize=1)
def piper_voice():
    if not PIPER_MODEL.is_file() or not PIPER_MODEL.with_suffix(PIPER_MODEL.suffix + ".json").is_file():
        raise VoiceUnavailable(f"Falta la voz Piper local: {PIPER_MODEL} y su archivo .onnx.json")
    try:
        from piper import PiperVoice
    except ImportError as exc:
        raise VoiceUnavailable("Falta piper-tts. Instala requirements.txt.") from exc
    return PiperVoice.load(str(PIPER_MODEL))


def transcribe(audio: bytes, suffix: str = ".webm") -> str:
    """Transcribe el audio recibido sin enviar datos fuera del equipo."""
    if not audio:
        return ""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
        temporary.write(audio)
        input_path = temporary.name
    try:
        segments, _ = whisper_model().transcribe(
            input_path,
            language="es",
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()
    finally:
        Path(input_path).unlink(missing_ok=True)


def synthesize_wav(text: str) -> bytes:
    """Genera WAV PCM en memoria, listo para reproducirse en el navegador."""
    if not text.strip():
        return b""
    try:
        from piper import SynthesisConfig
    except ImportError as exc:
        raise VoiceUnavailable("Falta piper-tts. Instala requirements.txt.") from exc
    voice = piper_voice()  # valida el modelo antes de abrir el contenedor WAV
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        if PIPER_SPEED <= 0:
            raise VoiceUnavailable("PIPER_SPEED debe ser mayor que 0.")
        if PIPER_NOISE_SCALE < 0:
            raise VoiceUnavailable("PIPER_NOISE_SCALE no puede ser negativo.")
        # En Piper, length_scale es inversamente proporcional a la velocidad.
        config = SynthesisConfig(
            speaker_id=PIPER_SPEAKER_ID,
            length_scale=1 / PIPER_SPEED,
            noise_scale=PIPER_NOISE_SCALE,
        )
        voice.synthesize_wav(text.strip(), wav_file, syn_config=config)
    return output.getvalue()
