"""API keys: the environment first, then one file readable only by its owner.

The file is `~/.lecture2audio.env` (or `L2A_KEYS_FILE`; `off` turns it off, as the tests do),
lines of `NAME=value`. `load()` runs once when the program starts (command line or app) and
copies every key whose variable is not set into the environment, so the rest of the code
reads keys from the environment only and each provider still reads only its own variable. A
variable set in the shell wins, so a key can be overridden for one run.

Like ssh with a private key, a file that its group or others can read is refused, not read.
The file is written atomically, created with mode 600. No system keychain is touched.
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

# Provider or search engine -> the variable its key is read from (llm.PRESETS, web.py).
KEY_ENV = {"deepseek": "DEEPSEEK_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
           "openai": "OPENAI_API_KEY", "kimi": "MOONSHOT_API_KEY",
           "openrouter": "OPENROUTER_API_KEY", "compat": "L2A_COMPAT_API_KEY",
           "local": "L2A_LOCAL_API_KEY", "tavily": "TAVILY_API_KEY", "brave": "BRAVE_API_KEY"}
KNOWN = set(KEY_ENV.values())


class KeyFileError(Exception):
    """The key file cannot be used: turned off, unsafe permissions, or a bad key."""


def path() -> Path | None:
    value = os.environ.get("L2A_KEYS_FILE", "~/.lecture2audio.env")
    return None if value in ("", "off", "0") else Path(value).expanduser()


def available() -> bool:
    """Whether keys can be saved."""
    return path() is not None


def variable(provider: str) -> str:
    if provider not in KEY_ENV:
        raise KeyFileError(f"unknown provider '{provider}' (one of: {', '.join(KEY_ENV)})")
    return KEY_ENV[provider]


def _read(file: Path) -> dict[str, str]:
    """The keys of the file; refuses a file others can read or that is not a plain file."""
    try:
        info = file.lstat()
    except FileNotFoundError:
        return {}
    if not stat.S_ISREG(info.st_mode):
        raise KeyFileError(f"{file} is not a regular file")
    if info.st_mode & 0o077:
        raise KeyFileError(f"{file} can be read by others; run: chmod 600 {file}")
    if info.st_uid != os.getuid():
        raise KeyFileError(f"{file} belongs to another user")
    keys = {}
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.removeprefix("export ").split("=", 1)
        name, value = name.strip(), value.strip().strip("'\"")
        if name in KNOWN and value:
            keys[name] = value
    return keys


def _write(file: Path, keys: dict[str, str]) -> None:
    file.parent.mkdir(parents=True, exist_ok=True)
    text = "# API keys of lecture2audio. Keep this file private (chmod 600).\n" + "".join(
        f"{name}={value}\n" for name, value in sorted(keys.items()))
    fd, tmp = tempfile.mkstemp(dir=file.parent, prefix=".lecture2audio-keys.")  # mode 600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
        os.replace(tmp, file)
    except BaseException:
        os.unlink(tmp)
        raise


def _file() -> Path:
    file = path()
    if file is None:
        raise KeyFileError("the key file is turned off (L2A_KEYS_FILE=off)")
    return file


def stored(var: str) -> bool:
    """Whether the file holds a key for this variable (False when it cannot be read)."""
    try:
        return var in _read(_file())
    except (KeyFileError, OSError):
        return False


def save(provider: str, key: str) -> str:
    """Saves a key in the file and in this process's environment; returns its variable."""
    key = key.strip()
    if not key or any(c.isspace() for c in key) or "\x00" in key or "'" in key or '"' in key:
        raise KeyFileError("the key is empty or holds spaces or quotes")
    var = variable(provider)
    file = _file()
    keys = _read(file)
    keys[var] = key
    _write(file, keys)
    os.environ[var] = key
    _loaded.add(var)
    return var


def delete(provider: str) -> bool:
    """Removes a key from the file; False when none was stored."""
    var = variable(provider)
    file = _file()
    keys = _read(file)
    if keys.pop(var, None) is None:
        return False
    _write(file, keys)
    if var in _loaded:  # the key came from the file: this process stops using it too
        _loaded.discard(var)
        os.environ.pop(var, None)
    return True


_loaded: set[str] = set()


def source(var: str) -> str | None:
    """Where the key in use comes from: 'file', 'shell', or None. Never the key itself."""
    if var in _loaded:
        return "file"
    return "shell" if os.environ.get(var) else None


def load() -> list[str]:
    """Copies the file's keys into the environment where the variable is unset; returns their
    variables. An unsafe file is not read, and says why on stderr."""
    file = path()
    if file is None:
        return []
    try:
        keys = _read(file)
    except (KeyFileError, OSError) as exc:
        print(f"warning: keys not loaded: {exc}", file=sys.stderr)
        return []
    found = []
    for var, key in keys.items():
        if not os.environ.get(var):
            os.environ[var] = key
            _loaded.add(var)
            found.append(var)
    return found
