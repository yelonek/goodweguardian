"""Dashboard musi serwować dashboard_ui.html + dashboard.js, nie legacy inline HTML."""

from fastapi.testclient import TestClient


def test_index_uses_external_ui_file() -> None:
    from guardian_dashboard import app

    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert 'id="mainNav"' in body
    assert 'id="page-overview"' in body
    assert 'src="/dashboard.js"' in body
    assert "<script>" not in body
    assert "advanced-panel" not in body
    assert "Current state" not in body

    js = client.get("/dashboard.js")
    assert js.status_code == 200
    assert "application/javascript" in js.headers.get("content-type", "")
    assert "function navigate(" in js.text
    assert "class=\"tag\">ACTIVE" in js.text
