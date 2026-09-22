"""Loading and lifetime of expensive shared data.

A dataset resource costs real memory and seconds to build (parsing a multi-GB
waveform, elaborating a netlist) and is cheap to share, so the server keeps at
most one live copy per identity and hands out references to it. Three rules make
that safe:

* one loader per key: a second asker waits for the load in progress instead of
  building a parallel copy;
* nothing is destroyed while a reference is held, so closing the session that
  named a resource cannot pull it out from under a running query;
* a failed load is never cached, because the file may have been mid-write and a
  remembered failure would make it permanent.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Optional


class ResourceLease:
    """One reference to a resource, released exactly once.

    Idempotent on purpose: a request that releases in a ``finally`` and again on
    an error path must not drive the count negative, or the resource would be
    destroyed while somebody still holds it.
    """

    __slots__ = ("_registry", "_key", "_resource", "_released", "_lock")

    def __init__(self, registry: "ResourceRegistry", key: Any,
                 resource: Any) -> None:
        self._registry = registry
        self._key = key
        self._resource = resource
        self._released = False
        self._lock = threading.Lock()

    @property
    def resource(self) -> Any:
        return self._resource

    def release(self) -> bool:
        """Drop the reference. False when it was already released."""
        with self._lock:
            if self._released:
                return False
            self._released = True
        self._registry.release(self._key, self._resource)
        return True

    def __enter__(self) -> Any:
        return self._resource

    def __exit__(self, *_exc: Any) -> bool:
        self.release()
        return False


class _Entry:
    __slots__ = ("loading", "resource", "error", "refcount")

    def __init__(self) -> None:
        self.loading = False
        self.resource: Any = None
        self.error: Optional[BaseException] = None
        self.refcount = 0


class ResourceRegistry:
    """Key -> at most one live resource, with single-flight loading."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._entries: Dict[Any, _Entry] = {}
        self._loads = 0
        self._destroys = 0
        self._close_errors = 0

    # -- acquiring ----------------------------------------------------------
    def acquire(self, key: Any, loader: Callable[[], Any]) -> Any:
        """Return the resource for ``key``, loading it only when nobody has it."""
        while True:
            with self._cond:
                entry = self._entries.get(key)
                if entry is None:
                    entry = _Entry()
                    entry.loading = True
                    self._entries[key] = entry
                    self._loads += 1
                    break            # lead the load, and do it outside the lock
                if entry.loading:
                    # Somebody is already building this exact key: wait for their
                    # result rather than paying for a second copy.
                    while entry.loading:
                        self._cond.wait()
                    if entry.error is not None:
                        raise entry.error
                    entry.refcount += 1
                    return entry.resource
                entry.refcount += 1
                return entry.resource

        try:
            resource = loader()      # no lock held: loading can take minutes
        except BaseException as exc:
            with self._cond:
                entry.error = exc
                entry.loading = False
                if self._entries.get(key) is entry:
                    del self._entries[key]
                self._cond.notify_all()
            raise

        with self._cond:
            entry.resource = resource
            entry.refcount = 1
            entry.loading = False
            self._cond.notify_all()
        return resource

    def retain(self, key: Any, resource: Any) -> Optional[ResourceLease]:
        """Add a reference to a resource that is already live."""
        with self._cond:
            entry = self._entries.get(key)
            if entry is None or entry.loading or entry.resource is not resource:
                return None
            entry.refcount += 1
        return ResourceLease(self, key, resource)

    # -- releasing ----------------------------------------------------------
    def release(self, key: Any, resource: Any) -> bool:
        """Drop one reference. True when this call destroyed the resource."""
        destroy = False
        with self._cond:
            entry = self._entries.get(key)
            if entry is None or entry.resource is not resource:
                # Never owned here, or already released. Refusing to decrement is
                # what makes a stray double release harmless.
                return False
            entry.refcount -= 1
            if entry.refcount <= 0:
                del self._entries[key]
                self._destroys += 1
                destroy = True
        if destroy:
            self._close(resource)
        return destroy

    def _close(self, resource: Any) -> None:
        close = getattr(resource, "close", None)
        if close is None:
            return
        try:
            close()
        except Exception:            # pylint: disable=broad-except
            # The entry is already gone, so a failing close cannot leave the
            # registry holding a dead resource; count it so it is not invisible.
            with self._cond:
                self._close_errors += 1

    # -- inspection ---------------------------------------------------------
    def refcount(self, key: Any) -> int:
        with self._cond:
            entry = self._entries.get(key)
            return entry.refcount if entry is not None else 0

    def stats(self) -> Dict[str, int]:
        with self._cond:
            live = [e for e in self._entries.values() if e.resource is not None]
            return {"keys": len(self._entries),
                    "loading": sum(1 for e in self._entries.values() if e.loading),
                    "refs": sum(e.refcount for e in live),
                    "loads": self._loads,
                    "destroys": self._destroys,
                    "close_errors": self._close_errors}
