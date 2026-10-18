"""`trenchnet config`: show and change trenchnet.toml from the command line.

An edit keeps the file's comments and layout: a setting's line is rewritten
in place, a new one goes at the end of its section, and `default` removes
it. The edited file has to load before it is saved, so a typo never leaves a
broken config behind.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import tomllib
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any

from .config import Config, _apply, check_value
from .textfile import read_text_any

RESET_WORDS = ("default", "reset")
_HEADER = re.compile(r"^\s*\[\s*([A-Za-z0-9_.-]+)\s*\]\s*(#.*)?$")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*=")  # a word that is itself section.key=value


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


def _is_setting(name: str) -> bool:
    try:
        resolve(name)
        return True
    except SettingError:
        return False


def parse_value(setting: Setting, raw: str) -> Any:
    """The value typed after '=', checked against the setting's type. None means back to the default."""
    raw = raw.strip()
    if not raw:
        raise SettingError(f"give {setting.name} a value after '=' (or =default to go back to the default)")
    if raw.lower() in RESET_WORDS:
        return None
    try:
        value = tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        value = raw  # a bare word, e.g. auto or claude-haiku-4-5
    if setting.type.replace(" | None", "").strip() == "str" and not isinstance(value, str):
        value = raw
    try:
        return check_value(setting.name, setting.type, value)
    except ValueError as e:
        raise SettingError(str(e)) from None


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
        if joined:
            _, eq, value = joined[-1].partition("=")
            if (not eq and w.startswith("=")) or (eq and not value and not _ASSIGNMENT.match(w)):
                joined[-1] += w
                continue
        joined.append(w)
    out: list[tuple[str, str | None]] = []
    for w in joined:
        name, eq, value = w.partition("=")
        out.append((name, value if eq else None))
    return out


def load_text(text: str) -> tuple[Config, list[str], bool]:
    """(the config with every setting that loads applied, what's wrong with the others,
    whether the text is valid TOML at all)."""
    cfg = Config()
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return cfg, [f"it isn't valid TOML ({e})"], False
    problems = []
    for key, value in data.items():
        parts = value.items() if isinstance(value, dict) and is_dataclass(getattr(cfg, key, None)) else None
        for one in ([{key: value}] if parts is None else [{key: {k: v}} for k, v in parts]):
            try:
                _apply(cfg, one, "")
            except ValueError as e:
                problems.append(str(e))
    return cfg, problems, True


def read_file(path: Path) -> tuple[str, Config, list[str], bool]:
    """(file text, then load_text's results; an absent file is empty and all defaults)."""
    if not path.exists():
        return "", Config(), [], True
    text = read_text_any(path)
    return (text, *load_text(text))


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


def _value_end(lines: list[str], start: int) -> int | None:
    """The last line of the setting that starts at `start` (a list can span several lines)."""
    for end in range(start, min(len(lines), start + 200)):
        try:
            tomllib.loads("\n".join(lines[start:end + 1]))
            return end
        except tomllib.TOMLDecodeError:
            continue
    return None


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
        end = _value_end(lines, idx) if idx is not None else None
        if idx is not None and end is None:
            continue  # can't tell where its value ends: the check after the edit refuses it
        if value is None:
            if idx is not None:
                del lines[idx:end + 1]
            continue
        new = f"{setting.key} = {toml_value(value)}"
        if idx is not None:
            old = lines[idx]
            indent = old[:len(old) - len(old.lstrip())]
            col = _comment_col(old) if end == idx else None
            if col is not None:
                new = (indent + new).ljust(col) if len(indent + new) < col else indent + new + "  "
                lines[idx:end + 1] = [new + old[col:]]
            else:
                lines[idx:end + 1] = [indent + new]
        elif span is not None:
            content = [i for i in range(*span) if lines[i].strip() and not lines[i].lstrip().startswith("#")]
            if content:  # after the section's last setting (all of it, when its value spans lines)
                at = content[-1] + 1
            elif setting.section is None:  # after the comments at the top of the file
                at = span[1]
                while at > 0 and not lines[at - 1].strip():
                    at -= 1
            else:
                at = span[0]
            lines.insert(at, new)
        else:
            lines += (["", f"[{setting.section}]", new] if lines else [f"[{setting.section}]", new])
    return "\n".join(lines) + "\n"


def _changes(words: list[str]) -> list[tuple[Setting, Any]]:
    pairs = split_assignments(words)
    for n, (name, value) in enumerate(pairs):
        if value is not None:
            continue
        nxt = pairs[n + 1] if n + 1 < len(pairs) else None
        if name in sections() and nxt is not None and nxt[1] is not None and "." not in nxt[0]:
            raise SettingError(f"write the section and the setting together: {name}.{nxt[0]}={nxt[1]}")
        if not _is_setting(name):
            prev = next((k for k in range(n - 1, -1, -1) if pairs[k][1] is not None), None)
            if prev is not None:
                words_after = []
                for word, v in pairs[prev + 1:]:
                    if v is not None or _is_setting(word):
                        break
                    words_after.append(word)
                p_name, p_value = pairs[prev]
                raise SettingError(f"'{name}' looks like part of the value of {p_name}: put quotes around a "
                                   f"value with spaces, e.g. {p_name}='{' '.join([p_value, *words_after])}'")
            resolve(name)  # raises: no such setting
        s = resolve(name)
        raise SettingError(f"give {s.name} a value, e.g. {s.name}=... (or {s.name}=default to go back to the default)")
    changes = [(resolve(name), raw) for name, raw in pairs]
    seen: dict[str, str] = {}
    for s, raw in changes:
        if s.name in seen:
            raise SettingError(f"{s.name} is given twice ({seen[s.name]} and {raw}); give it once")
        seen[s.name] = raw
    return [(s, parse_value(s, raw)) for s, raw in changes]


def change(path: Path, words: list[str]) -> list[str]:
    """Apply `section.key=value` words to the file. Returns lines describing what changed."""
    changes = _changes(words)
    text, before, problems, readable = read_file(path)
    if not readable:
        raise SettingError(f"{path} can't be read: {problems[0]}. Fix that line in a text editor first")
    if "�" in text:
        raise SettingError(f"{path} has characters that couldn't be read; save it as UTF-8 in your text editor "
                           "first, so changing it doesn't damage them")
    if not text:
        text = ("# TRENCHNET settings. Every key is optional; anything not here uses its default.\n"
                "# Change settings with: trenchnet config section.key=value\n")
    new_text = edit_text(text, changes)
    after, new_problems, readable = load_text(new_text)
    if not readable:
        raise SettingError(f"couldn't edit {path} automatically; nothing was changed. "
                           "Edit it in a text editor instead.")
    caused = [q for q in new_problems if q not in problems]
    if caused:
        raise SettingError(f"couldn't edit {path} automatically ({caused[0]}); nothing was changed. "
                           "Edit it in a text editor instead.")
    if new_problems:
        raise SettingError(f"{path}: {new_problems[0]}. Fix that line in a text editor first (nothing was changed)")
    for s, value in changes:  # e.g. a "[section]" line inside a multi-line string, which isn't a section
        expected = s.default if value is None else value
        if s.value_in(after) != expected:
            raise SettingError(f"couldn't edit {s.name} in {path} automatically; nothing was changed. "
                               "Edit it in a text editor instead.")
    # Written next to it, then swapped in: a failed write (full disk) leaves the old file as it was.
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    out = []
    for s, value in changes:
        old, new = s.value_in(before), s.value_in(after)
        note = " (the default)" if new == s.default else f" (default: {toml_value(s.default)})"
        verb = "unchanged" if old == new else f"{toml_value(old)} -> {toml_value(new)}"
        out.append(f"{s.name}: {verb}{note}")
    return out


def describe(path: Path, words: list[str]) -> list[str]:
    """What `trenchnet config [section | section.key ...]` prints."""
    text, cfg, problems, readable = read_file(path)
    out = [f"! {path}: {p}" for p in problems]
    if problems:
        out.append("  the bot won't start until that's fixed; "
                   + ("the values below are the defaults" if not readable else "other settings are shown as loaded"))
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
        elif path.exists() and not problems:
            out.append("every setting is the default")
        out += ["",
                "see a section:  trenchnet config " + " | ".join(sections()),
                "change:         trenchnet config strategy.min_tracked_buyers=2",
                "undo a change:  trenchnet config strategy.min_tracked_buyers=default"]
        return out
    previous: Setting | None = None
    for word in words:
        if word in sections():
            out.append(f"[{word}]")
            rows = [(s.key, s) for s in known.values() if s.section == word]
            previous = None
        else:
            if previous is not None and not _is_setting(word):
                raise SettingError(f"to change {previous.name}, write {previous.name}={word}")
            s = previous = resolve(word)
            rows = [(s.name, s)]
        width = max(len(label) for label, _ in rows)
        for label, s in rows:
            value = s.value_in(cfg)
            mark = "" if value == s.default else f"   (changed; default: {toml_value(s.default)})"
            out.append(f"  {label:<{width}} = {toml_value(value)}{mark}")
    return out
