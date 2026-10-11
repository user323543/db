"""pytest-Konfiguration: Fake-EFA-Server und Flask-Testclient."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_efa import FakeEfa  # noqa: E402  (nach sys.path-Anpassung)
import app as nrw_app  # noqa: E402


@pytest.fixture()
def fake_efa():
    """Läuft nur während eines Tests; liefert den laufenden Fake-Server."""
    server = FakeEfa()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture()
def client(fake_efa, monkeypatch):
    """Flask-Testclient, dessen Backend auf den Fake-EFA zeigt."""
    monkeypatch.setattr(nrw_app, "EFA_BASE_URL", fake_efa.base_url)
    nrw_app._cache.clear()
    with nrw_app.app.test_client() as test_client:
        yield test_client
    nrw_app._cache.clear()
