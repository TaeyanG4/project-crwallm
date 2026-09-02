"""Bytes to text.

Worth its own module because getting it wrong is silent: the crawl succeeds,
the records are stored, and every Korean field is mojibake. Korean sites still
serve EUC-KR and CP949 in 2026, often while declaring UTF-8, so the order of
evidence matters more than any single source.

Precedence, strongest evidence first:

1. **BOM** - unambiguous, and it overrides a contradicting declaration
2. **Content-Type charset** - what the server says it sent
3. **``<meta charset>`` / ``<meta http-equiv>``** - what the document says
4. **UTF-8 trial** - succeeds or fails cleanly, no guessing
5. **CP949** - the Korean fallback; supersets EUC-KR
6. **Windows-1252, replacing errors** - never raises, so decoding always ends
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass

__all__ = ["DecodedDocument", "decode_html"]

_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""", re.I)
_META_CONTENT_TYPE = re.compile(
    rb"""<meta[^>]+http-equiv\s*=\s*["']?content-type["']?[^>]*content\s*=\s*["'][^"']*charset\s*=\s*([a-zA-Z0-9_\-]+)""",
    re.I,
)

_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)

# Declared name -> what to actually use. Servers name the subset and serve the
# superset; decoding with the declared name then fails on perfectly good text.
_ALIASES = {
    "euc-kr": "cp949",
    "euckr": "cp949",
    "ks_c_5601-1987": "cp949",
    "ksc5601": "cp949",
    "iso-8859-1": "cp1252",
    "latin-1": "cp1252",
    "latin1": "cp1252",
    "ascii": "utf-8",
    "us-ascii": "utf-8",
    "shift_jis": "cp932",
    "sjis": "cp932",
    "gb2312": "gb18030",
    "gbk": "gb18030",
}

# Only the head is scanned for a meta declaration: a charset that appears
# 100KB into the body is not one the browser used either.
_META_SCAN_BYTES = 4096


@dataclass(frozen=True, slots=True)
class DecodedDocument:
    text: str
    encoding: str
    declared_encoding: str | None
    """What the server or document claimed, before aliasing. ``None`` when
    nothing was declared and the encoding was determined by trial."""

    @property
    def was_guessed(self) -> bool:
        return self.declared_encoding is None


def _normalise(name: str | None) -> str | None:
    if not name:
        return None
    key = name.strip().strip("\"'").lower()
    return _ALIASES.get(key, key)


def _charset_from_content_type(content_type: str | None) -> str | None:
    if not content_type or "charset=" not in content_type.lower():
        return None
    return content_type.lower().split("charset=", 1)[1].split(";", 1)[0].strip()


def _charset_from_meta(body: bytes) -> str | None:
    head = body[:_META_SCAN_BYTES]
    for pattern in (_META_CONTENT_TYPE, _META_CHARSET):
        match = pattern.search(head)
        if match:
            return match.group(1).decode("ascii", errors="ignore")
    return None


def _try(body: bytes, encoding: str) -> str | None:
    try:
        return body.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return None


def decode_html(body: bytes, content_type: str | None = None) -> DecodedDocument:
    """Decode ``body``, never raising.

    A crawler that throws on a badly encoded page loses the page; one that
    substitutes replacement characters keeps most of it. The final fallback
    therefore always succeeds, and ``was_guessed`` records that it happened.
    """
    if not body:
        return DecodedDocument("", "utf-8", None)

    for bom, encoding in _BOMS:
        if body.startswith(bom):
            decoded = _try(body, encoding)
            if decoded is not None:
                return DecodedDocument(decoded, encoding, encoding)

    declared_raw = _charset_from_content_type(content_type) or _charset_from_meta(body)
    declared = _normalise(declared_raw)

    if declared:
        decoded = _try(body, declared)
        if decoded is not None:
            return DecodedDocument(decoded, declared, declared_raw)
        # A wrong declaration is common enough that it is not worth failing
        # over - fall through and work it out from the bytes.

    for candidate in ("utf-8", "cp949"):
        decoded = _try(body, candidate)
        if decoded is not None:
            return DecodedDocument(decoded, candidate, declared_raw)

    return DecodedDocument(body.decode("cp1252", errors="replace"), "cp1252", declared_raw)
