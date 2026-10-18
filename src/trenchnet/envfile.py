"""Read keys from a .env file, so `source .env` isn't needed before each command."""

from __future__ import annotations

import logging
import os
import re
import shlex
from pathlib import Path

from .textfile import read_text_any

log = logging.getLogger(__name__)

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ASSIGN = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def load_env_file(path: str | Path = ".env") -> list[str]:
    """Set environment variables from a .env file: the `export NAME="value"`
    lines written for Git Bash, plain `NAME=value`, or several assignments on
    one `export` line. A variable already set in the environment is left as
    it is, so a key typed in the terminal wins. Returns the names it set."""
    p = Path(path)
    if not p.is_file():
        return []
    try:
        text = read_text_any(p)
    except OSError as e:
        log.warning("could not read %s (%s)", p, e.strerror)
        return []
    found: list[tuple[str, str]] = []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip().lstrip("﻿")
        if not line or line.startswith("#"):
            continue
        try:
            words = shlex.split(line, comments=True)
        except ValueError:
            log.warning("%s line %d can't be read (check its quotes); skipped", p, n)
            continue
        if words and words[0] == "export":
            words = words[1:]
        pairs = [w.partition("=") for w in words]
        if pairs and all(eq and _NAME.fullmatch(name) for name, eq, _ in pairs):
            found += [(name, value) for name, _, value in pairs]
            continue
        m = _ASSIGN.match(line)  # e.g. spaces around "=": NAME = "value"
        if m:
            found.append((m.group(1), m.group(2).strip().strip("\"'")))
        else:
            log.warning("%s line %d isn't NAME=value; skipped", p, n)
    loaded = []
    for name, value in found:
        if name not in os.environ:
            os.environ[name] = value
            loaded.append(name)
    return loaded
