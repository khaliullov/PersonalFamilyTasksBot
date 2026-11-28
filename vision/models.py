"""Pydantic v2 data contracts for Vision analysis results and photo workflow."""

from enum import Enum
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class VisionCategory(str, Enum):
    SHOPPING = "shopping"
    HOME = "home"
    REPAIR = "repair"
    DOCUMENT = "document"
    RECEIPT = "receipt"
    SCREENSHOT = "screenshot"
    PERSON = "person"
    OTHER = "other"


_ALLOWED_CATEGORY_VALUES = {c.value for c in VisionCategory}


def _clean_str_list(value: Any, max_items: int = 50, max_item_len: int = 500) -> List[str]:
    """Normalizes list-like inputs into deduplicated clean strings."""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []

    cleaned: List[str] = []
    seen = set()
    for item in value:
        if item is None:
            continue
        text = str(item).strip()
        if not text:
            continue
        if len(text) > max_item_len:
            text = text[:max_item_len].rstrip() + "..."
        key = text.lower()
        if key not in seen:
            seen.add(key)
            cleaned.append(text)
        if len(cleaned) >= max_items:
            break
    return cleaned


def _clean_optional_str(value: Any, max_len: int = 1000) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "n/a", "неизвестно", "нет"}:
        return None
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "..."
    return text


class VisionAnalysisResult(BaseModel):
    """Validated structured output produced by the local Vision model."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    image_description: str = Field(..., min_length=1, max_length=2500)
    short_summary: str = Field(default="", max_length=300)
    visible_text: List[str] = Field(default_factory=list)
    objects: List[str] = Field(default_factory=list)
    brands_and_models: List[str] = Field(default_factory=list)
    measurements: List[str] = Field(default_factory=list)
    condition_or_problem: Optional[str] = Field(default=None, max_length=1000)
    category: VisionCategory = Field(default=VisionCategory.OTHER)
    task_candidate: bool = Field(default=False)
    suggested_title: Optional[str] = Field(default=None, max_length=200)
    suggested_details: Optional[str] = Field(default=None, max_length=1500)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    uncertainties: List[str] = Field(default_factory=list)
    requires_clarification: bool = Field(default=False)
    clarification_question: Optional[str] = Field(default=None, max_length=500)

    @field_validator("image_description", mode="before")
    @classmethod
    def _validate_description(cls, value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("image_description must be a non-empty string")
        return text[:2500]

    @field_validator("short_summary", mode="before")
    @classmethod
    def _validate_short_summary(cls, value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()[:300]

    @field_validator(
        "visible_text",
        "objects",
        "brands_and_models",
        "measurements",
        "uncertainties",
        mode="before",
    )
    @classmethod
    def _validate_lists(cls, value: Any) -> List[str]:
        return _clean_str_list(value)

    @field_validator(
        "condition_or_problem",
        "suggested_title",
        "suggested_details",
        "clarification_question",
        mode="before",
    )
    @classmethod
    def _validate_optional_texts(cls, value: Any) -> Optional[str]:
        return _clean_optional_str(value)

    @field_validator("category", mode="before")
    @classmethod
    def _validate_category(cls, value: Any) -> VisionCategory:
        if isinstance(value, VisionCategory):
            return value
        raw = str(value or "other").strip().lower()
        if raw in _ALLOWED_CATEGORY_VALUES:
            return VisionCategory(raw)
        return VisionCategory.OTHER

    @field_validator("task_candidate", "requires_clarification", mode="before")
    @classmethod
    def _validate_bools(cls, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes")
        return bool(value)

    @field_validator("confidence", mode="before")
    @classmethod
    def _validate_confidence(cls, value: Any) -> float:
        try:
            num = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"confidence must be a float between 0.0 and 1.0, got {value!r}") from exc
        if num < 0.0 or num > 1.0:
            raise ValueError(f"confidence out of range [0.0, 1.0]: {num}")
        return round(num, 4)

    def ensure_summary(self) -> "VisionAnalysisResult":
        """Returns self with a guaranteed non-empty short_summary derived from description if omitted."""
        if not self.short_summary:
            fallback = self.image_description.split(".")[0].strip()
            if len(fallback) > 140:
                fallback = fallback[:137].rstrip() + "..."
            return self.model_copy(update={"short_summary": fallback})
        return self


class AlbumImageContext(BaseModel):
    """Represents a single analyzed image within a message or album batch."""

    message_id: int
    file_unique_id: Optional[str] = None
    caption: Optional[str] = None
    analysis: VisionAnalysisResult
