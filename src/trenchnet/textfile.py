"""Reading text files made on Windows."""

from __future__ import annotations

import codecs
import locale
from pathlib import Path


def read_text_any(path: str | Path) -> str:
    """Text saved by Notepad, Excel or PowerShell: UTF-8 (with or without a
    BOM), UTF-16, or this PC's Windows ANSI code page (cp1252 in Western
    Europe and the Americas, cp1251 for Cyrillic, ...). Line endings come back as
    "\n", as text-mode open() gives them. Raises OSError if the file can't
    be read."""
    raw = Path(path).read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = _decode_ansi(raw)
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _decode_ansi(raw: bytes) -> str:
    try:
        encoding = codecs.lookup(locale.getpreferredencoding(False)).name
        if encoding != "utf-8":
            return raw.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        pass
    return raw.decode("cp1252", errors="replace")
