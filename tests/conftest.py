"""Shared pytest configuration: the default suite must never touch the network."""

from __future__ import annotations

import socket
from collections.abc import Callable
from typing import Any

import pytest

# Loopback is not the network: on Windows, asyncio's event loop (used by Streamlit's AppTest)
# builds its self-pipe from a socket pair connected over 127.0.0.1.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _loopback_only(real: Callable[..., Any]) -> Callable[..., Any]:
    def connect(sock: socket.socket, address: Any, *args: Any, **kwargs: Any) -> Any:
        host = address[0] if isinstance(address, tuple) and address else None
        if host in _LOOPBACK_HOSTS:
            return real(sock, address, *args, **kwargs)
        raise RuntimeError("Network access is disabled in the test suite")

    return connect


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if any test tries to open a non-loopback socket connection."""
    monkeypatch.setattr(socket.socket, "connect", _loopback_only(socket.socket.connect))
    monkeypatch.setattr(socket.socket, "connect_ex", _loopback_only(socket.socket.connect_ex))
