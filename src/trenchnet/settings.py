"""`trenchnet config`: show and change trenchnet.toml from the command line.

An edit keeps the file's comments and layout: a setting's line is rewritten
in place, a new one goes at the end of its section, and `default` removes
it. The edited file has to load before it is saved, so a typo never leaves a
broken config behind.
"""

from __future__ import annotations

import difflib
import json
import math
import re
import tomllib
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any

from .config import Config, _apply
from .textfile import read_text_any

RESET_WORDS = ("default", "reset")
_HEADER = re.compile(r"^\s*\[\s*([A-Za-z0-9_.-]+)\s*\]\s*(#.*)?$")
_KEY_LINE = re.compile(r"^\s*[A-Za-z0-9_-]+\s*=")


class SettingError(ValueError):
    pass


@dataclass(frozen=True)
class Setting:
    section: str | None  # None: a top-level key like db_path
    key: str
    type: str  # the dataclass annotation, e.g. "int", "float | None"
    default: Any

    @property
    def name(self) -> str:
        return f"{self.section}.{self.key}" if self.section else self.key

    def value_in(self, cfg: Config) -> Any:
        return getattr(getattr(cfg, self.section) if self.section else cfg, self.key)


def all_settings() -> dict[str, Setting]:
    out: dict[str, Setting] = {}
    cfg = Config()
    for f in fields(cfg):
        value = getattr(cfg, f.name)
        if is_dataclass(value):
            for g in fields(value):
                out[f"{f.name}.{g.name}"] = Setting(f.name, g.name, str(g.type), getattr(value, g.name))
        else:
            out[f.name] = Setting(None, f.name, str(f.type), value)
    return out


def sections() -> list[str]:
    return [f.name for f in fields(Config) if is_dataclass(getattr(Config(), f.name))]


def resolve(name: str) -> Setting:
    """'strategy.min_tracked_buyers', or just 'min_tracked_buyers' when only one section has it."""
    known = all_settings()
    name = name.strip()
    if name in known:
        return known[name]
    matches = [s for s in known.values() if s.key == name]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise SettingError(f"{name} is in more than one section; say which: "
                           + " or ".join(s.name for s in matches))
    close = difflib.get_close_matches(name, list(known) + [s.key for s in known.values()], n=3, cutoff=0.6)
    names = dict.fromkeys(n for c in close for n in ([c] if c in known else
                                                      [s.name for s in known.values() if s.key == c]))
    hint = f" Did you mean {' or '.join(names)}?" if names else ""
    raise SettingError(f"there is no setting called {name}.{hint} "
                       "See them all with: trenchnet config " + " / ".join(sections()))


def parse_value(setting: Setting, raw: str) -> Any:
    """The value typed after '=', checked against the setting's type. None means back to the default."""
    raw = raw.strip()
    if raw.lower() in RESET_WORDS:
        return None
    try:
        value = tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        value = raw  # a bare word, e.g. auto or claude-haiku-4-5
    kind = setting.type.replace(" | None", "").strip()
    number = isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "str":
        return value if isinstance(value, str) else raw
    if kind == "bool":
        if isinstance(value, bool):
            return value
        raise SettingError(f"{setting.name} must be true or false, not {raw}")
    if kind in ("int", "float"):
        if not number or not math.isfinite(value):
            raise SettingError(f"{setting.name} must be a number, not {raw}")
        if value < 0:
            raise SettingError(f"{setting.name} can't be negative")
        if kind == "int":
            if value != int(value):
                raise SettingError(f"{setting.name} must be a whole number, not {raw}")
            return int(value)
        return value
    if kind == "list[list[float]]":
        ok = isinstance(value, list) and all(
            isinstance(p, list) and len(p) == 2
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0 for x in p) for p in value)
        if not ok:
            raise SettingError(f"{setting.name} must be a list of [multiple, fraction] pairs, "
                               "e.g. [[2.0, 0.5], [4.0, 0.25]]")
        return value
    return value


def toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # JSON string escapes are valid TOML
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    if v is None:
        return "not set"
    return str(v)


def split_assignments(words: list[str]) -> list[tuple[str, str | None]]:
    """['a.b=1', 'c.d', '=', '2', 'e'] -> [('a.b', '1'), ('c.d', '2'), ('e', None)].
    Spaces around '=' arrive as separate words."""
    joined: list[str] = []
    for w in words:
        if joined and (joined[-1].endswith("=") or w.startswith("=")):
            joined[-1] += w
        else:
            joined.append(w)
    out: list[tuple[str, str | None]] = []
    for w in joined:
        name, eq, value = w.partition("=")
        out.append((name, value if eq else None))
    return out


def read_file(path: Path) -> tuple[str, Config, str | None]:
    """(file text, loaded config, problem with it if it doesn't load)."""
    if not path.exists():
        return "", Config(), None
    text = read_text_any(path)
    cfg = Config()
    try:
        _apply(cfg, tomllib.loads(text), "")
    except (tomllib.TOMLDecodeError, ValueError) as e:
        return text, Config(), str(e)
    return text, cfg, None


def _comment_col(line: str) -> int | None:
    """Where a trailing # comment starts, ignoring # inside quoted strings."""
    quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return i
        i += 1
    return None


def _spans(lines: list[str]) -> dict[str | None, tuple[int, int]]:
    """section -> (first line after its header, its end); None is the part before the first header."""
    spans: dict[str | None, tuple[int, int]] = {}
    current, start = None, 0
    for i, line in enumerate(lines):
        m = _HEADER.match(line)
        if m:
            spans.setdefault(current, (start, i))
            current, start = m.group(1), i + 1
    spans.setdefault(current, (start, len(lines)))
    return spans


def edit_text(text: str, changes: list[tuple[Setting, Any]]) -> str:
    """Apply changes ((setting, value or None for the default)) to a TOML text."""
    lines = text.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    for setting, value in changes:
        spans = _spans(lines)
        span = spans.get(setting.section)
        key = re.compile(rf"^\s*{re.escape(setting.key)}\s*=")
        idx = next((i for i in range(*span) if key.match(lines[i])), None) if span else None
        if value is None:
            if idx is not None:
                del lines[idx]
            continue
        new = f"{setting.key} = {toml_value(value)}"
        if idx is not None:
            old = lines[idx]
            indent = old[:len(old) - len(old.lstrip())]
            col = _comment_col(old)
            if col is not None:
                new = (indent + new).ljust(col) if len(indent + new) < col else indent + new + "  "
                lines[idx] = new + old[col:]
            else:
                lines[idx] = indent + new
        elif span is not None:
            settings_lines = [i for i in range(*span) if _KEY_LINE.match(lines[i])]
            if settings_lines:
                at = settings_lines[-1] + 1
            elif setting.section is None:  # after the comments at the top of the file
                at = next((i for i in range(*span) if lines[i].strip() and not lines[i].lstrip().startswith("#")),
                          span[1])
                while at > 0 and not lines[at - 1].strip():
                    at -= 1
            else:
                at = span[0]
            lines.insert(at, new)
        else:
            lines += (["", f"[{setting.section}]", new] if lines else [f"[{setting.section}]", new])
    return "\n".join(lines) + "\n"


def change(path: Path, words: list[str]) -> list[str]:
    """Apply `section.key=value` words to the file. Returns lines describing what changed."""
    pairs = split_assignments(words)
    missing = [name for name, value in pairs if value is None]
    if missing:
        raise SettingError(f"give {missing[0]} a value, e.g. {resolve(missing[0]).name}=... "
                           "(or =default to go back to the default)")
    changes = [(s, parse_value(s, raw)) for s, raw in ((resolve(name), raw) for name, raw in pairs)]
    text, before, problem = read_file(path)
    if problem:
        raise SettingError(f"{path} doesn't load ({problem}); fix that line by hand first")
    if not text:
        text = ("# TRENCHNET settings. Every key is optional; anything not here uses its default.\n"
                "# Change settings with: trenchnet config section.key=value\n")
    new_text = edit_text(text, changes)
    after = Config()
    try:
        _apply(after, tomllib.loads(new_text), "")
    except (tomllib.TOMLDecodeError, ValueError) as e:
        raise SettingError(f"couldn't edit {path} automatically ({e}); nothing was changed. "
                           "Edit it in a text editor instead.") from e
    for s, value in changes:  # e.g. a value spread over several lines that the edit couldn't replace
        expected = s.default if value is None else value
        if s.value_in(after) != expected:
            raise SettingError(f"couldn't edit {s.name} in {path} automatically; nothing was changed. "
                               "Edit it in a text editor instead.")
    path.write_text(new_text, encoding="utf-8")
    out = []
    for s, value in changes:
        old, new = s.value_in(before), s.value_in(after)
        note = " (the default)" if new == s.default else f" (default: {toml_value(s.default)})"
        verb = "unchanged" if old == new else f"{toml_value(old)} -> {toml_value(new)}"
        out.append(f"{s.name}: {verb}{note}")
    return out


def describe(path: Path, words: list[str]) -> list[str]:
    """What `trenchnet config [section | section.key ...]` prints."""
    text, cfg, problem = read_file(path)
    out = []
    if problem:
        out.append(f"! {path} doesn't load ({problem}); the bot would stop at startup. Fix that line by hand.")
    known = all_settings()
    if not words:
        where = f"settings file: {path.resolve()}" if path.exists() else \
            f"no {path} yet in {Path.cwd()}: every setting is the default (a change creates the file)"
        out.append(where)
        changed = [s for s in known.values() if s.value_in(cfg) != s.default]
        if changed:
            out.append("changed from the defaults:")
            width = max(len(f"{s.name} = {toml_value(s.value_in(cfg))}") for s in changed)
            out += [f"  {f'{s.name} = {toml_value(s.value_in(cfg))}':<{width}}   (default: {toml_value(s.default)})"
                    for s in changed]
        elif path.exists():
            out.append("every setting is the default")
        out += ["",
                "see a section:  trenchnet config " + " | ".join(sections()),
                "change:         trenchnet config strategy.min_tracked_buyers=2",
                "undo a change:  trenchnet config strategy.min_tracked_buyers=default"]
        return out
    for word in words:
        if word in sections():
            out.append(f"[{word}]")
            rows = [(s.key, s) for s in known.values() if s.section == word]
        else:
            s = resolve(word)
            rows = [(s.name, s)]
        width = max(len(label) for label, _ in rows)
        for label, s in rows:
            value = s.value_in(cfg)
            mark = "" if value == s.default else f"   (changed; default: {toml_value(s.default)})"
            out.append(f"  {label:<{width}} = {toml_value(value)}{mark}")
    return out
