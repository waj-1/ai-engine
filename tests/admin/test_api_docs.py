"""Tests for protected API docs at /admin/docs/api* and disabled public docs."""


def test_public_docs_disabled(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_protected_api_docs_served(client):
    # `client` fixture installs the superuser override.
    assert client.get("/admin/docs/api").status_code == 200
    spec = client.get("/admin/docs/api/openapi.json")
    assert spec.status_code == 200
    assert spec.json()["info"]["title"] == "WAJ AI Engine API"


def test_redoc_served(client):
    assert client.get("/admin/docs/api/redoc").status_code == 200
