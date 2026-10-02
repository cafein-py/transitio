"""Credentials for feeds that need an account with their provider.

The installed feed index lists the providers that issue credentials
(:class:`transitio.index.AccessProvider`) and the fields each one issues.
Each field is looked up on its own: first in the environment variable
``TRANSITIO_KEY_<PROVIDER>__<FIELD>`` (upper-cased, ``-`` as ``_``, an empty
value counting as unset), then in the credentials file
``<user config dir>/transitio/credentials.toml``, one table per provider::

    [gcba-transporte]
    client_id = "..."
    client_secret = "..."

The file and its directory must be private to the user (mode ``0600`` and
a directory closed to group and others for writing) and neither may be a
symbolic link; :func:`set` creates them that way. On Windows there is no
credentials file: the environment variables are the only store.
"""

import contextlib
import os
import re
import secrets
import stat
import sys
from collections.abc import Mapping
from pathlib import Path

import platformdirs

from transitio.catalog._access import _QUERY_VALUE, _Secret

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

__all__ = ["clear", "configured", "get", "set"]

FILE_NAME = "credentials.toml"
_MAX_FILE_BYTES = 1024 * 1024
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_ESCAPED = re.compile(r'["\\\x00-\x1f\x7f]')
_POSITION = re.compile(r"\(at line \d+, column \d+\)|\(at end of document\)")


def set(provider_id, fields, *, index=None, path=None):
    """Store credentials for a provider in the credentials file.

    ``fields`` maps each credential field the provider issues to its value,
    a non-empty string; the provider's other stored fields and the other
    providers stay as they are. ``index`` is the feed index listing the
    provider (the installed one by default) and ``path`` the credentials
    file (``credentials.toml`` in the user config directory by default).
    """
    _refuse_on_windows()
    values = _checked(_provider(provider_id, index), fields)
    file = _file(path)
    with _directory(file.parent, create=True, lock=True) as dir_fd:
        tables, problem = _read(dir_fd, file)
        if problem is not None:
            raise problem
        tables.setdefault(provider_id, {}).update(values)
        _write(dir_fd, file.name, tables)


def get(provider_id, *, index=None, path=None):
    """The provider's credentials as ``{field: value}``, or None when any
    field it issues is not set; ``index`` and ``path`` as for :func:`set`."""
    fields, missing = _resolve(_provider(provider_id, index), path=path)
    if missing:
        return None
    return {field: secret.reveal() for field, secret in fields.items()}


def clear(provider_id=None, *, path=None):
    """Remove a provider's credentials from the credentials file, or the
    whole file when ``provider_id`` is None; environment variables stay
    set. Clearing a provider the file does not hold does nothing."""
    _refuse_on_windows()
    file = _file(path)
    with _directory(file.parent, lock=True) as dir_fd:
        if dir_fd is None:
            return
        if provider_id is None:
            if _exists(dir_fd, file):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(file.name, dir_fd=dir_fd)
            return
        tables, problem = _read(dir_fd, file)
        if problem is not None:
            raise problem
        if tables.pop(provider_id, None) is not None:
            _write(dir_fd, file.name, tables)


def configured(*, index=None, path=None):
    """The providers of the index whose credential fields are all set, as
    ``{provider_id: source}``: ``"env"``, ``"file"``, or ``"mixed"`` when
    some fields come from each. Values are never returned."""
    index = _with_providers(index)
    stored = _stored(path)
    sources = {}
    for provider_id in dict.fromkeys(index.access_providers["provider_id"]):
        provider = index.access_provider(provider_id)
        found = _lookup(provider, None, stored)
        if provider.credential_fields and not _missing(provider, found):
            kinds = {source for _, source in found.values()}
            sources[provider_id] = kinds.pop() if len(kinds) == 1 else "mixed"
    return sources


def _resolve(provider, explicit=None, *, path=None):
    """``(fields, missing)`` for an :class:`~transitio.index.AccessProvider`:
    each field that resolves as a :class:`_Secret`, and the sorted names of
    those that do not. ``explicit`` maps fields to values that win over the
    environment and the file."""
    found = _lookup(provider, explicit, _stored(path))
    fields = {field: secret for field, (secret, _) in found.items()}
    return fields, _missing(provider, found)


def _lookup(provider, explicit, stored):
    """Each field that resolves, with the secret and its source, in the
    provider's field order."""
    explicit = explicit or {}
    table = stored.get(provider.provider_id, {})
    found = {}
    for field, name in provider.env_names.items():
        env = os.environ.get(name)
        if field in explicit:
            value = explicit[field]
            secret = value if isinstance(value, _Secret) else _Secret(value)
            found[field] = (secret, "explicit")
        elif env:
            found[field] = (_Secret(env), "env")
        elif field in table:
            found[field] = (table[field], "file")
    return found


def _missing(provider, found):
    return sorted(field for field in provider.credential_fields if field not in found)


def _checked(provider, fields):
    """``fields`` as ``{field: _Secret}`` after checking each is a field the
    provider issues with a non-empty string value."""
    if not isinstance(fields, Mapping):
        raise TypeError("fields must map credential fields to values")
    if not fields:
        raise ValueError(f"no credential fields given for {provider.provider_id!r}")
    issued = ", ".join(provider.credential_fields) or "none"
    values = {}
    for field, value in fields.items():
        if field not in provider.credential_fields:
            raise ValueError(
                f"{provider.provider_id!r} issues no credential field {field!r}; "
                f"it issues {issued}"
            )
        # Text UTF-8 can encode, which is what a TOML file holds.
        if not isinstance(value, str) or not _QUERY_VALUE.fullmatch(value):
            raise ValueError(f"credential {field!r} must be a non-empty string")
        values[field] = _Secret(value)
    return values


def _with_providers(index):
    from transitio.index import _coerce_index

    index = _coerce_index(index)
    if index.access_providers is None:
        raise ValueError("the installed index has no access providers; refresh it")
    return index


def _provider(provider_id, index):
    provider = _with_providers(index).access_provider(provider_id)
    if provider is None:
        raise ValueError(f"the index lists no credential provider {provider_id!r}")
    return provider


def _refuse_on_windows():
    if sys.platform == "win32":
        raise NotImplementedError(
            "transitio keeps no credentials file on Windows; set the "
            "TRANSITIO_KEY_<PROVIDER>__<FIELD> environment variables instead"
        )


def _file(path):
    if path is None:
        path = Path(platformdirs.user_config_dir("transitio")) / FILE_NAME
    return Path(os.path.abspath(path))


def _stored(path):
    """The credentials file as ``{provider_id: {field: _Secret}}``; empty
    when it does not exist, and on Windows, where it is never read."""
    if sys.platform == "win32":
        return {}
    file = _file(path)
    with _directory(file.parent) as dir_fd:
        if dir_fd is None:
            return {}
        tables, problem = _read(dir_fd, file)
    if problem is not None:
        raise problem
    return tables


@contextlib.contextmanager
def _directory(directory, *, create=False, lock=False):
    """A descriptor of the file's directory, None when it is missing and not
    created; a directory created here has mode 0700. With ``lock`` it is held
    under an exclusive lock, so updates of the file run one at a time."""
    if create:
        os.makedirs(directory.parent, exist_ok=True)
        with contextlib.suppress(FileExistsError):
            os.mkdir(directory, 0o700)
    try:
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        if create:
            raise
        dir_fd = None
    except OSError:
        if os.path.islink(directory):
            raise PermissionError(f"{directory}: a symbolic link, not a directory")
        raise
    if dir_fd is None:
        yield None
        return
    try:
        info = os.fstat(dir_fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise PermissionError(
                f"{directory}: not owned by you or writable by others; "
                f"run chmod 700 {directory}"
            )
        mode = stat.S_IMODE(info.st_mode)
        if create and mode & 0o700 != 0o700:
            # The umask may have taken the owner's own access away.
            os.fchmod(dir_fd, mode | 0o700)
        if lock:
            import fcntl

            fcntl.flock(dir_fd, fcntl.LOCK_EX)
        yield dir_fd
    finally:
        os.close(dir_fd)


def _exists(dir_fd, file):
    """Whether ``file`` is in ``dir_fd``; a symbolic link or anything but a
    regular file there is refused."""
    try:
        info = os.stat(file.name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        raise PermissionError(f"{file}: a symbolic link, not a regular file")
    if not stat.S_ISREG(info.st_mode):
        raise PermissionError(f"{file}: not a regular file")
    return True


def _read(dir_fd, file):
    """``(tables, problem)`` for the file ``file`` in ``dir_fd``; a malformed
    file is returned as the error to raise, not raised here, so no frame
    holding its text is kept by a traceback."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(file.name, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        return {}, None
    except OSError:
        _exists(dir_fd, file)
        raise
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PermissionError(f"{file}: not a regular file")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise PermissionError(
                f"{file}: readable or writable by others; run chmod 600 {file}"
            )
        data = stream.read(_MAX_FILE_BYTES + 1)
    if len(data) > _MAX_FILE_BYTES:
        return None, ValueError(f"{file}: over {_MAX_FILE_BYTES} bytes")
    return _parse(data, file)


def _parse(data, file):
    """``(tables, problem)`` for the file's bytes: the problem names the
    file and the offending key, never a value."""
    try:
        document = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        position = _POSITION.search(str(error))
        where = f" {position.group()}" if position else ""
        return None, ValueError(f"{file}: not a valid TOML file{where}")
    tables = {}
    for provider_id, table in document.items():
        if not isinstance(table, dict):
            return None, ValueError(f"{file}: {provider_id!r} is not a table")
        for field, value in table.items():
            if not isinstance(value, str) or not value:
                return None, ValueError(
                    f"{file}: {provider_id}.{field} is not a non-empty string"
                )
        tables[provider_id] = {field: _Secret(value) for field, value in table.items()}
    return tables, None


def _write(dir_fd, name, tables):
    """Replace the file ``name`` in ``dir_fd`` with ``tables``, written to a
    new 0600 file first and renamed over it; a file too large to read back is
    refused before anything is written."""
    data = bytearray(_dumps(tables).encode("utf-8"))
    temporary = f".credentials-{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        if len(data) > _MAX_FILE_BYTES:
            raise ValueError(f"the credentials would take over {_MAX_FILE_BYTES} bytes")
        fd = os.open(temporary, flags, 0o600, dir_fd=dir_fd)
        try:
            os.fchmod(fd, 0o600)
            written = 0
            while written < len(data):
                written += os.write(fd, data[written:])
            os.fsync(fd)
            os.replace(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary, dir_fd=dir_fd)
            raise
        finally:
            os.close(fd)
    finally:
        # A traceback keeps this frame's locals: the text goes before it does.
        del data[:]


def _dumps(tables):
    """TOML for ``{table: {key: _Secret}}``: one table each, values as basic
    strings."""
    lines = []
    for table, fields in tables.items():
        lines += ["", f"[{_key(table)}]"] if lines else [f"[{_key(table)}]"]
        lines += [f"{_key(k)} = {_string(v.reveal())}" for k, v in fields.items()]
    return "".join(f"{line}\n" for line in lines)


def _key(key):
    return key if _BARE_KEY.fullmatch(key) else _string(key)


def _string(text):
    escaped = _ESCAPED.sub(lambda match: f"\\u{ord(match.group()):04x}", text)
    return f'"{escaped}"'
