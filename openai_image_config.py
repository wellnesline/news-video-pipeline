"""synthetic stub: image generation defaults (constants only)."""
DEFAULT_IMAGE_MODEL = "gpt-image-2"
DEFAULT_IMAGE_QUALITY = "low"


def validate_image_defaults(model: str = DEFAULT_IMAGE_MODEL, quality: str = DEFAULT_IMAGE_QUALITY) -> None:
    """Placeholder validator: only checks that both values are non-empty strings."""
    if not model or not quality:
        raise ValueError("image model/quality must be set")
