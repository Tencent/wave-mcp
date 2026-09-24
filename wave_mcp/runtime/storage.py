"""Where wave-mcp is allowed to write, and how.

Three layers, one resolver (dev-docs/planning/开发标准-身份-文件位置-参数.md,
section 2):

    session artefacts   user-visible, reusable, travel with the design
                        (``session.json``, ``netlist/``)
                        -> the caller's ``out_dir`` when given, exactly as given;
                           otherwise ``<session_root>/<dataset identity>``, where
                           session_root is ``$WAVE_MCP_SESSION_ROOT`` |
                           ``~/.wave-mcp/sessions``
    converted waveform  ``<dir>/<name>.fst`` (+ ``.fst.hier`` for FSDB) next to
                        ``<dir>/<name>.vcd|.fsdb``, where users keep converted
                        dumps anyway (``convert.cached_fst``, standard 2.8)
    derived caches      rebuildable from the inputs, losing them only costs time
                        (FSTs that cannot sit beside their source, conversion
                        records, netlist module stores, built helpers)
                        -> ``$WAVE_MCP_CACHE_ROOT`` | ``~/.wave-mcp/cache``
    temporary           lives for one call (FIFOs, logs, probes)
                        -> ``tempfile``; the caller deletes it

Nothing else is a valid destination. Beside an input only the converted FST
itself may appear: its record and lock live in the cache, and it is written
under a hidden temporary name then renamed. An unwritable source directory or
a partial conversion sends the FST to the cache, with a notice. Cache writes
are atomic (temp name, fsync, rename) and serialized per key with a lock file,
so two processes converting the same waveform build it once.

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
import socket
import sys
import tempfile
import time
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
    def locked(self, directory: str, *, what: str = "",
               wait: Optional[float] = None) -> Iterator[None]:
        """Hold ``<directory>/.lock`` exclusively for the block.

        Waits for the lock rather than failing: the other holder is building
        the very artefact this caller wants, so waiting and then re-checking
        the cache is the cheap path. The wait is never silent: once the lock
        turns out to be busy, a line on stderr names ``what`` is being waited
        for, the lock file and the holder (pid@host, written by every holder),
        repeated every 30 s. ``wait`` (seconds) bounds it: past that,
        :class:`LockBusy` is raised with the same details, so a holder that
        never lets go (a wedged process, a lock stuck on a network
        filesystem) fails the call instead of hanging it.
        """
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, LOCK_NAME)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            _acquire(fd, path, what or directory, wait)
            _write_holder(fd)
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


class LockBusy(TimeoutError):
    """A lock stayed held by another process past the allowed wait."""


#: seconds between "still waiting" lines while a lock is busy
_LOCK_NOTE_INTERVAL = 30.0


def _note(msg: str) -> None:
    try:
        sys.stderr.write(msg + "\n")
        sys.stderr.flush()
    except Exception:  # pylint: disable=broad-except
        pass


def _holder(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(200).strip()
        return text.splitlines()[0] if text else "unknown"
    except OSError:
        return "unknown"


def _write_holder(fd: int) -> None:
    """Record ``pid@host`` in the lock file, for whoever waits on it next."""
    try:
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"pid {os.getpid()}@{socket.gethostname()}\n".encode(), 0)
    except OSError:
        pass


def _acquire(fd: int, path: str, what: str, wait: Optional[float]) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    except BlockingIOError:
        pass
    start = time.monotonic()
    holder = _holder(path)
    limit = f", giving up after {wait:.0f}s" if wait else ""
    _note(f"[wave-mcp] waiting for {what}: lock {path} is held by {holder}"
          f"{limit}")
    last_note = start
    delay = 0.2
    while True:
        time.sleep(delay)
        delay = min(delay * 1.5, 2.0)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _note(f"[wave-mcp] lock acquired after "
                  f"{time.monotonic() - start:.0f}s: {path}")
            return
        except BlockingIOError:
            pass
        now = time.monotonic()
        if wait and now - start >= wait:
            raise LockBusy(
                f"gave up after {now - start:.0f}s waiting for {what}: lock "
                f"{path} is still held by {_holder(path)}. If that process "
                f"is gone or stuck, stop it; a lock left behind on a network "
                f"filesystem clears once its client releases it, or delete "
                f"{path} when nothing is using it.")
        if now - last_note >= _LOCK_NOTE_INTERVAL:
            _note(f"[wave-mcp] still waiting ({now - start:.0f}s) for {what}: "
                  f"lock {path} held by {_holder(path)}")
            last_note = now


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


#: marker file a session directory is touched through on every open
LAST_USED_NAME = ".last_used"


def mark_used(session_dir: str) -> None:
    """Record that a session was just opened (mtime of ``.last_used``).

    Only inside wave-mcp's own session root: a session the caller placed with
    an explicit ``out_dir`` is theirs, and ``gc`` never touches it anyway.
    """
    try:
        root = os.path.abspath(default_session_root())
        d = os.path.abspath(session_dir)
        if os.path.dirname(d) != root or not os.path.isdir(d):
            return
        p = os.path.join(d, LAST_USED_NAME)
        with open(p, "a"):
            pass
        os.utime(p, None)
    except OSError:
        pass


def _tree_size(path: str) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


def _last_used(path: str) -> float:
    for name in (LAST_USED_NAME, "session.json"):
        try:
            return os.path.getmtime(os.path.join(path, name))
        except OSError:
            continue
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def usage_entries() -> list:
    """Every session directory and cache entry wave-mcp owns, with size/age.

    Sessions: direct children of the session root. Caches: direct children of
    each ``<cache_root>/<kind>/`` directory. Entries named explicitly by a
    caller (``out_dir``) live elsewhere and are never listed.
    """
    import time as _time
    now = _time.time()
    out = []
    sroot = default_session_root()
    if os.path.isdir(sroot):
        for name in sorted(os.listdir(sroot)):
            p = os.path.join(sroot, name)
            if os.path.isdir(p) and not os.path.islink(p):
                out.append({"kind": "session", "path": p,
                            "bytes": _tree_size(p),
                            "idle_days": (now - _last_used(p)) / 86400.0})
    croot = default_cache_root()
    if os.path.isdir(croot):
        for kind in sorted(os.listdir(croot)):
            kd = os.path.join(croot, kind)
            if not os.path.isdir(kd) or os.path.islink(kd):
                continue
            for name in sorted(os.listdir(kd)):
                p = os.path.join(kd, name)
                if os.path.islink(p) or name == LOCK_NAME:
                    continue
                size = _tree_size(p) if os.path.isdir(p) else os.path.getsize(p)
                out.append({"kind": f"cache/{kind}", "path": p, "bytes": size,
                            "idle_days": (now - _last_used(p) if os.path.isdir(p)
                                          else now - os.path.getmtime(p)) / 86400.0})
    return out


def gc(older_than_days: Optional[float] = None,
       max_total_bytes: Optional[int] = None, dry_run: bool = True,
       keep: Optional[set] = None) -> dict:
    """Remove idle sessions / cache entries under wave-mcp's own roots.

    ``older_than_days`` removes entries idle longer than that. ``max_total_bytes``
    then removes least-recently-used entries until the total fits. Nothing is
    removed with ``dry_run`` (the default). ``keep`` lists paths never removed
    (sessions open in this process). Only direct children of the two roots are
    candidates, so a symlink or a caller's ``out_dir`` is never followed.
    """
    import shutil
    keep = {os.path.abspath(k) for k in (keep or set())}
    entries = [e for e in usage_entries() if os.path.abspath(e["path"]) not in keep]
    doomed = []
    if older_than_days is not None:
        doomed = [e for e in entries if e["idle_days"] > older_than_days]
    if max_total_bytes is not None:
        rest = sorted((e for e in entries if e not in doomed),
                      key=lambda e: -e["idle_days"])
        total = sum(e["bytes"] for e in rest)
        for e in rest:
            if total <= max_total_bytes:
                break
            doomed.append(e)
            total -= e["bytes"]
    freed = 0
    removed = []
    for e in doomed:
        if not dry_run:
            try:
                if os.path.isdir(e["path"]):
                    shutil.rmtree(e["path"])
                else:
                    os.remove(e["path"])
            except OSError:
                continue
        freed += e["bytes"]
        removed.append(e)
    total_before = sum(e["bytes"] for e in entries)
    return {"dry_run": dry_run, "session_root": default_session_root(),
            "cache_root": default_cache_root(),
            "entries": len(entries), "total_bytes": total_before,
            "removed": removed, "freed_bytes": freed}


__all__ = ["SESSION_ROOT_ENV", "CACHE_ROOT_ENV", "LOCK_NAME",
           "StoragePolicy", "policy", "user_path", "default_root",
           "default_cache_root", "default_session_root", "mark_used",
           "usage_entries", "gc", "LAST_USED_NAME"]
