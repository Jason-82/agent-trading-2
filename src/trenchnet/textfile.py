"""Reading text files made on Windows."""

from __future__ import annotations

from pathlib import Path


def read_text_any(path: str | Path) -> str:
    """Text saved by Notepad, Excel or PowerShell: UTF-8 (with or without a
    BOM), UTF-16, or the Windows ANSI code page. Line endings come back as
    "\n", as text-mode open() gives them. Raises OSError if the file can't
    be read."""
    raw = Path(path).read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")
