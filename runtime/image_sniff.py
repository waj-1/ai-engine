"""Image type detection from magic bytes.

Agent-neutral: chat photos (runtime/chat_images.py) and document-photo
scanners both need it, and neither should have to import the other.
"""
from __future__ import annotations

from typing import Optional

_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis"}
_HEIF_GENERIC_BRANDS = {b"mif1", b"msf1", b"heif"}


def sniff_image_mime(data: bytes) -> Optional[str]:
    """Best-effort MIME from magic bytes, for uploads whose declared type is
    missing or ``application/octet-stream`` (some phone browsers send HEIC
    photos that way). Returns None when the bytes are not a supported image.
    """
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in _HEIF_BRANDS:
            return "image/heic"
        if brand in _HEIF_GENERIC_BRANDS:
            return "image/heif"
    return None
