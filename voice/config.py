import os
from typing import Set

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _bool_from_env(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on", "y")


# ==============================================================================
# SPEECH-TO-TEXT (ASR) CONFIGURATION
# ==============================================================================

# Enable or disable local voice processing
ASR_ENABLED: bool = _bool_from_env("ASR_ENABLED", True)

# Whisper model size (tiny, base, small, medium, large-v3)
ASR_MODEL_SIZE: str = os.getenv("ASR_MODEL_SIZE", "small")

# Device for inference: cpu, cuda, auto
ASR_DEVICE: str = os.getenv("ASR_DEVICE", "cpu")

# Compute type: int8, float16, float32, default
ASR_COMPUTE_TYPE: str = os.getenv("ASR_COMPUTE_TYPE", "int8")

# Default transcription language
ASR_LANGUAGE: str = os.getenv("ASR_LANGUAGE", "ru")

# Beam size for faster-whisper beam search
ASR_BEAM_SIZE: int = int(os.getenv("ASR_BEAM_SIZE", "5"))

# Timeout for transcription inference in seconds
ASR_TIMEOUT_SECONDS: int = int(os.getenv("ASR_TIMEOUT_SECONDS", "300"))

# Maximum allowed voice file size in MB
ASR_MAX_FILE_SIZE_MB: int = int(os.getenv("ASR_MAX_FILE_SIZE_MB", "30"))


# ==============================================================================
# VOICE SUMMARY & STORAGE CONFIGURATION
# ==============================================================================

# Summary display mode: 'db_only' (store in SQLite only) or 'edit_caption' (append to original voice message caption)
VOICE_SUMMARY_MODE: str = os.getenv("VOICE_SUMMARY_MODE", "db_only").strip().lower()
ALLOWED_SUMMARY_MODES: Set[str] = {"db_only", "edit_caption"}
if VOICE_SUMMARY_MODE not in ALLOWED_SUMMARY_MODES:
    VOICE_SUMMARY_MODE = "db_only"

# Directory where audio files are saved when retention is enabled
VOICE_AUDIO_DIR: str = os.getenv("VOICE_AUDIO_DIR", "voice_audio")

# Retain original audio on disk? If False, temporary audio files are removed after transcription
VOICE_KEEP_AUDIO: bool = _bool_from_env("VOICE_KEEP_AUDIO", False)
