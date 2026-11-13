"""Shared pytest configuration: the default suite must never touch the network."""

from __future__ import annotations

import socket
from typing import NoReturn

import pytest


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if any test tries to open a socket connection."""

    def _refuse(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("Network access is disabled in the test suite")

    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)
