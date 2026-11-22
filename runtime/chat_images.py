"""Photos on ``POST /chat/stream``.

``ChatImage`` is deliberately a plain model with NO content validation: content
is checked in :func:`decode_images`, so a bad image becomes the contract's
``422 {"error": "invalid_image", ...}`` and never a pydantic
``RequestValidationError`` (whose body is FastAPI's generic one).
"""
from __future__ import annotations

import base64
import binascii
from typing import Literal

from pydantic import BaseModel

from runtime.image_sniff import sniff_image_mime

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 4 * 1024 * 1024
ALLOWED_MIMES = ("image/jpeg", "image/png", "image/webp")

InvalidImageReason = Literal["too_large", "unsupported_type", "too_many", "decode_failed"]


class ChatImage(BaseModel):
    mime: str
    data_base64: str


class InvalidImage(Exception):
    """A chat image failed validation. ``index`` is 0-based."""

    def __init__(self, index: int, reason: InvalidImageReason):
        super().__init__(f"invalid image {index}: {reason}")
        self.index = index
        self.reason = reason


def decode_images(images: list[ChatImage]) -> list[tuple[bytes, str]]:
    """Decode and validate; raise :class:`InvalidImage` on the first failure.

    Order: count, then per image declared type, base64, size, magic bytes.
    """
    if len(images) > MAX_IMAGES:
        raise InvalidImage(MAX_IMAGES, "too_many")
    out: list[tuple[bytes, str]] = []
    for i, img in enumerate(images):
        if img.mime not in ALLOWED_MIMES:
            raise InvalidImage(i, "unsupported_type")
        try:
            data = base64.b64decode(img.data_base64, validate=True)
        except (binascii.Error, ValueError):
            raise InvalidImage(i, "decode_failed") from None
        if not data:
            raise InvalidImage(i, "decode_failed")
        if len(data) > MAX_IMAGE_BYTES:
            raise InvalidImage(i, "too_large")
        if sniff_image_mime(data) != img.mime:
            raise InvalidImage(i, "unsupported_type")
        out.append((data, img.mime))
    return out


def photo_placeholder(count: int) -> str:
    """The persisted stand-in for photos when the message text is empty.
    Always English, whatever the locale: clients match this exact form."""
    return "[1 photo attached]" if count == 1 else f"[{count} photos attached]"
