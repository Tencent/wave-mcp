"""Where wave-mcp is allowed to write, and how.

Three layers, one resolver (dev-docs/planning/开发标准-身份-文件位置-参数.md,
section 2):

    session artefacts   user-visible, reusable, travel with the design
                        (``session.json``, ``netlist/``)
                        -> the caller's ``out_dir`` when given, exactly as given;
                           otherwise ``<session_root>/<dataset identity>``, where
                           session_root is ``$WAVE_MCP_SESSION_ROOT`` |
                           ``~/.wave-mcp/sessions``
    derived caches      rebuildable from the inputs, losing them only costs time
                        (converted ``.fst``, msgpack sidecars, built helpers)
                        -> ``$WAVE_MCP_CACHE_ROOT`` | ``~/.wave-mcp/cache``
    temporary           lives for one call (FIFOs, logs, probes)
                        -> ``tempfile``; the caller deletes it

Nothing else is a valid destination. In particular the directory an input file
lives in is never written to: regression areas are shared and often read-only,
and a derived artefact next to somebody's dump is a surprise they did not ask
for. Cache writes are atomic (temp name, fsync, rename) and serialized per key
with a lock file, so two processes converting the same waveform build it once.

The two roots resolve the same way and mean the same thing: a default location
for what the caller did not place explicitly. Neither rewrites a path the
caller did give; what a path may or may not touch is the operating system's
decision, since the process runs as the user.

Every ``expanduser`` in the package lives here.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import tempfile
from typing import Callable, Iterator, Optional

from .identity import cache_key

#: Environment variable overriding the default session root.
SESSION_ROOT_ENV = "WAVE_MCP_SESSION_ROOT"
#: Environment variable overriding the derived-cache root.
CACHE_ROOT_ENV = "WAVE_MCP_CACHE_ROOT"
#: Name of the per-key lock file inside a cache directory.
LOCK_NAME = ".lock"


def user_path(raw: str, *, home_relative: bool = False) -> str:
    """Normalize a user-supplied path: ``~`` expanded, made absolute.

    The one sanctioned way to turn a caller's spelling of a location into a
    comparable path; ``~/x``, ``./x`` and ``/home/me/x`` naming one file must
    compare equal regardless of the server's working directory.

    ``home_relative=True`` anchors a relative value on ``$HOME`` instead of the
    working directory. Deployment-config values (MCP client ``env`` blocks)
    want this: the server is started from whatever project dir the client
    happens to be in, and a relative path there would silently miss.
    """
    expanded = os.path.expanduser(raw)
    if home_relative and not os.path.isabs(expanded):
        expanded = os.path.join(os.path.expanduser("~"), expanded)
    return os.path.abspath(expanded)


def _env_path(name: str) -> Optional[str]:
    raw = os.environ.get(name, "").strip()
    return user_path(raw, home_relative=True) if raw else None


def default_root() -> str:
    """The single default base for everything wave-mcp writes: ``~/.wave-mcp``."""
    return os.path.join(os.path.expanduser("~"), ".wave-mcp")


def default_cache_root() -> str:
    """``$WAVE_MCP_CACHE_ROOT`` | ``~/.wave-mcp/cache``."""
    explicit = _env_path(CACHE_ROOT_ENV)
    if explicit:
        return explicit
    return os.path.join(default_root(), "cache")


def default_session_root() -> str:
    """``$WAVE_MCP_SESSION_ROOT`` | ``~/.wave-mcp/sessions``.

    Sessions are data, not cache: they hold the elaborated netlist the user
    keeps coming back to. Both still live under one ``~/.wave-mcp`` root so
    everything the package ever writes is found (and cleaned) in one place.
    """
    explicit = _env_path(SESSION_ROOT_ENV)
    if explicit:
        return explicit
    return os.path.join(default_root(), "sessions")


class StoragePolicy:
    """Resolves every write location the package uses.

    Instances are cheap and read the environment at construction, so callers
    that must follow a changed environment (tests, long-lived servers whose
    config is re-read) take a fresh one from ``policy()``.
    """

    __slots__ = ("session_root", "cache_root")

    def __init__(self, session_root: Optional[str] = None,
                 cache_root: Optional[str] = None) -> None:
        self.session_root = session_root or default_session_root()
        self.cache_root = cache_root or default_cache_root()

    @classmethod
    def from_env(cls) -> "StoragePolicy":
        return cls(session_root=default_session_root(),
                   cache_root=default_cache_root())

    # -- layer 1: session artefacts -----------------------------------------
    def session_dir(self, out_dir: Optional[str], identity: str) -> str:
        """Where a session lands.

        ``out_dir`` given: used as given (normalised), nothing else. Not given:
        ``<session_root>/<identity>``, where ``identity`` is the dataset
        identity of the inputs, so the same design asked for from anywhere
        resolves to one directory and its netlist is reused, while two designs
        never share one.
        """
        if out_dir:
            return user_path(out_dir)
        if not identity:
            raise ValueError("a session without out_dir needs a dataset identity")
        return os.path.join(self.session_root, identity)

    # -- layer 2: derived caches --------------------------------------------
    def cache_dir(self, kind: str, *key_parts: str, create: bool = True) -> str:
        """``<cache_root>/<kind>/<digest of key_parts>``."""
        d = os.path.join(self.cache_root, kind)
        if key_parts:
            d = os.path.join(d, cache_key(*key_parts))
        if create:
            os.makedirs(d, exist_ok=True)
        return d

    @contextlib.contextmanager
    def locked(self, directory: str) -> Iterator[None]:
        """Hold ``<directory>/.lock`` exclusively for the block.

        Blocks until the lock is free rather than failing: the other holder is
        building the very artefact this caller wants, so waiting and then
        re-checking the cache is the cheap path.
        """
        os.makedirs(directory, exist_ok=True)
        fd = os.open(os.path.join(directory, LOCK_NAME),
                     os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @staticmethod
    def atomic_write(path: str, writer: Callable[[str], None]) -> None:
        """Have ``writer`` produce ``path`` under a temporary name, then rename.

        ``writer`` receives the temporary path and must create that file. A
        reader never sees a half-written artefact, and a crash leaves only a
        temp file next to it, which the next writer replaces.
        """
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
        os.close(fd)
        try:
            writer(tmp)
            _fsync_file(tmp)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(tmp)
            raise

    @staticmethod
    def atomic_write_bytes(path: str, data: bytes) -> None:
        def writer(tmp: str) -> None:
            with open(tmp, "wb") as fh:
                fh.write(data)
        StoragePolicy.atomic_write(path, writer)

    @staticmethod
    def discard(path: str) -> None:
        """Remove a cache artefact that turned out unusable; absence is fine."""
        try:
            os.remove(path)
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise


def _fsync_file(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def policy() -> StoragePolicy:
    """The policy for the current environment."""
    return StoragePolicy.from_env()


__all__ = ["SESSION_ROOT_ENV", "CACHE_ROOT_ENV", "LOCK_NAME",
           "StoragePolicy", "policy", "user_path", "default_root",
           "default_cache_root", "default_session_root"]
