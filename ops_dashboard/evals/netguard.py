"""Block outbound network for the duration of an offline eval run.

Every way the stdlib (and therefore httpx / requests / SDKs) opens a TCP/UDP
connection goes through ``socket.socket.connect``/``connect_ex``,
``socket.create_connection`` or ``socket.getaddrinfo``. We patch those for the
whole process (provider calls run in a worker thread, see
``brain.ask_with_deadline``) and record every attempt so the runner can fail the
case with ``network_blocked`` even if the caller swallowed the exception.
AF_UNIX sockets stay allowed (local IPC, never leaves the machine).
"""
from __future__ import annotations

import contextlib
import socket
import threading
from collections.abc import Iterator


class NetworkBlocked(RuntimeError):
    """Raised when code under an offline eval tries to reach the network."""


class _Guard:
    def __init__(self) -> None:
        self.attempts: list[str] = []
        self._lock = threading.Lock()

    def note(self, what: str) -> None:
        with self._lock:
            self.attempts.append(what)


@contextlib.contextmanager
def blocked() -> Iterator[_Guard]:
    guard = _Guard()
    orig_connect = socket.socket.connect
    orig_connect_ex = socket.socket.connect_ex
    orig_create = socket.create_connection
    orig_gai = socket.getaddrinfo

    def _is_unix(sock: socket.socket) -> bool:
        return getattr(socket, "AF_UNIX", None) is not None and sock.family == socket.AF_UNIX

    def connect(self: socket.socket, address):  # type: ignore[no-untyped-def]
        if _is_unix(self):
            return orig_connect(self, address)
        guard.note(f"connect {address!r}")
        raise NetworkBlocked(f"offline eval: network access blocked ({address!r})")

    def connect_ex(self: socket.socket, address):  # type: ignore[no-untyped-def]
        if _is_unix(self):
            return orig_connect_ex(self, address)
        guard.note(f"connect_ex {address!r}")
        raise NetworkBlocked(f"offline eval: network access blocked ({address!r})")

    def create_connection(address, *a, **k):  # type: ignore[no-untyped-def]
        guard.note(f"create_connection {address!r}")
        raise NetworkBlocked(f"offline eval: network access blocked ({address!r})")

    def getaddrinfo(host, *a, **k):  # type: ignore[no-untyped-def]
        guard.note(f"getaddrinfo {host!r}")
        raise NetworkBlocked(f"offline eval: DNS lookup blocked ({host!r})")

    socket.socket.connect = connect  # type: ignore[method-assign,assignment]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign,assignment]
    socket.create_connection = create_connection  # type: ignore[assignment]
    socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
    try:
        yield guard
    finally:
        socket.socket.connect = orig_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = orig_connect_ex  # type: ignore[method-assign]
        socket.create_connection = orig_create
        socket.getaddrinfo = orig_gai
