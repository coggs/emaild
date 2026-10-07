import base64

from fastapi.testclient import TestClient


def _client(monkeypatch, password):
    from emaild import config
    monkeypatch.setenv("EMAILD_WEB_PASSWORD", password)
    config.settings.cache_clear()
    from emaild.web.app import app
    return TestClient(app, raise_server_exceptions=False)


def test_password_required(monkeypatch):
    c = _client(monkeypatch, "s3cret")
    r = c.get("/api/status")
    assert r.status_code == 401
    assert "Basic" in r.headers["www-authenticate"]
    bad = base64.b64encode(b"x:nope").decode()
    assert c.get("/api/status", headers={"Authorization": f"Basic {bad}"}).status_code == 401


def test_correct_password_passes_gate(monkeypatch):
    c = _client(monkeypatch, "s3cret")
    good = base64.b64encode(b"anyone:s3cret").decode()
    # gate passes; the route itself needs a DB, so anything other than 401 proves the gate let it through
    r = c.get("/oauth/google/start", headers={"Authorization": f"Basic {good}"})
    assert r.status_code != 401
