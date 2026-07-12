"""Verrous inter-processus et persistance JSON transactionnelle (Linux)."""

from __future__ import annotations

import errno
import json
import os
import shutil
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:  # fcntl est volontairement la primitive de production demandée (Linux).
    import fcntl
except ImportError:  # pragma: no cover - le bot de production s'exécute sous Linux
    fcntl = None


_thread_lock_guard = threading.Lock()
_thread_locks: dict[str, threading.RLock] = {}
_bot_thread_locks: dict[str, threading.Lock] = {}


def _thread_lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _thread_lock_guard:
        return _thread_locks.setdefault(key, threading.RLock())


def _bot_thread_lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _thread_lock_guard:
        return _bot_thread_locks.setdefault(key, threading.Lock())


class LockUnavailableError(RuntimeError):
    pass


class BotLock:
    """Verrou exclusif consultable, libéré automatiquement à la mort du processus."""

    def __init__(self, path: str | Path, *, timeout: float = 10.0, version: str = "unknown"):
        self.path = Path(path)
        self.timeout = timeout
        self.version = version
        self._file = None
        self._thread_lock = _bot_thread_lock_for(self.path)

    @property
    def held(self) -> bool:
        return self._file is not None

    def acquire(self) -> None:
        if fcntl is None:
            raise LockUnavailableError("fcntl.flock indisponible : Linux est requis pour le verrou de production")
        if self.held:
            return
        if not self._thread_lock.acquire(timeout=self.timeout):
            raise LockUnavailableError(
                f"verrou déjà détenu dans ce processus pour {self.path.name} après {self.timeout:.1f}s"
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = self.path.open("a+", encoding="utf-8")
        except Exception:
            self._thread_lock.release()
            raise
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    handle.close()
                    self._thread_lock.release()
                    raise
                if time.monotonic() >= deadline:
                    handle.close()
                    self._thread_lock.release()
                    raise LockUnavailableError(
                        f"verrou déjà détenu pour {self.path.name} après {self.timeout:.1f}s"
                    ) from exc
                time.sleep(0.05)
        self._file = handle
        self.refresh_metadata()

    def refresh_metadata(self) -> None:
        if not self.held:
            return
        metadata = {
            "pid": os.getpid(),
            "started_at": int(time.time()),
            "hostname": socket.gethostname(),
            "version": self.version,
        }
        self._file.seek(0)
        self._file.truncate()
        json.dump(metadata, self._file, ensure_ascii=False)
        self._file.flush()
        os.fsync(self._file.fileno())

    def release(self) -> None:
        if not self.held:
            return
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None
            self._thread_lock.release()

    def __enter__(self) -> "BotLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


class AtomicJsonStateStore:
    """Lecture/écriture JSON atomique avec lock fichier et trois sauvegardes."""

    def __init__(self, path: str | Path, *, backup_count: int = 3):
        self.path = Path(path)
        self.lock_path = Path(f"{self.path}.state.lock")
        self.backup_count = backup_count

    @contextmanager
    def _locked(self, exclusive: bool) -> Iterator[None]:
        if fcntl is None:
            raise LockUnavailableError("fcntl.flock indisponible : Linux est requis pour le state")
        thread_lock = _thread_lock_for(self.lock_path)
        with thread_lock:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            with self.lock_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def read(self) -> dict[str, Any] | None:
        with self._locked(exclusive=False):
            for candidate in (self.path, *(Path(f"{self.path}.bak{i}") for i in range(1, self.backup_count + 1))):
                if not candidate.exists():
                    continue
                try:
                    with candidate.open("r", encoding="utf-8") as handle:
                        value = json.load(handle)
                    if isinstance(value, dict):
                        return value
                except (OSError, json.JSONDecodeError):
                    continue
        return None

    def write(self, data: dict[str, Any]) -> None:
        with self._locked(exclusive=True):
            self._rotate_backups()
            temporary = Path(f"{self.path}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
            try:
                with temporary.open("w", encoding="utf-8") as handle:
                    json.dump(data, handle, indent=2, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                self._fsync_directory()
            finally:
                if temporary.exists():
                    temporary.unlink()

    def _rotate_backups(self) -> None:
        if not self.path.exists():
            return
        for index in range(self.backup_count, 1, -1):
            older = Path(f"{self.path}.bak{index - 1}")
            newer = Path(f"{self.path}.bak{index}")
            if older.exists():
                os.replace(older, newer)
        shutil.copy2(self.path, Path(f"{self.path}.bak1"))

    def _fsync_directory(self) -> None:
        descriptor = os.open(str(self.path.parent or Path(".")), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
