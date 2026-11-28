"""Typed exception hierarchy for the Vision subsystem with transient/permanent classification."""


class VisionError(Exception):
    """Base exception for all Vision subsystem errors."""

    transient: bool = False

    def __init__(self, message: str, *, transient: bool | None = None) -> None:
        super().__init__(message)
        if transient is not None:
            self.transient = transient


class VisionConfigError(VisionError):
    """Permanent configuration error."""

    transient = False


class VisionImageError(VisionError):
    """Permanent image validation, corruption, size, or unsupported format error."""

    transient = False


class VisionDownloadError(VisionError):
    """Transient error downloading image from Telegram."""

    transient = True


class VisionUnavailableError(VisionError):
    """Transient error communicating with local Ollama endpoint."""

    transient = True


class VisionTimeoutError(VisionError):
    """Transient inference or HTTP timeout error."""

    transient = True


class VisionModelNotFoundError(VisionError):
    """Permanent error when the configured Ollama Vision model is not installed."""

    transient = False


class VisionParseError(VisionError):
    """Error parsing or validating structured JSON returned by Vision model."""

    transient = True
