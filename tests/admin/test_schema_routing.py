from unittest.mock import patch, MagicMock


def test_service_client_routes_to_ai_project_default_schema(monkeypatch):
    """The service client now targets the standalone AI project
    (AI_SUPABASE_*) and uses the DEFAULT public schema — the old
    `wajagents` schema override has been dropped."""
    monkeypatch.setenv("AI_SUPABASE_URL", "http://ai.127.0.0.1:54321")
    monkeypatch.setenv("AI_SUPABASE_SERVICE_KEY", "ai_svc")
    captured = {}

    def fake_create_client(url, key, options=None):
        captured["url"] = url
        captured["key"] = key
        captured["schema"] = getattr(options, "schema", None)
        return MagicMock()

    with patch("core.auth.create_client", side_effect=fake_create_client):
        from core.auth import get_service_client
        get_service_client()
    # Builds against the AI project, not the main project.
    assert captured["url"] == "http://ai.127.0.0.1:54321"
    assert captured["key"] == "ai_svc"
    # No wajagents override — default public schema.
    assert captured["schema"] in (None, "public")


def test_user_client_stays_public(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://127.0.0.1:54321")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    captured = {}

    def fake_create_client(url, key, options=None):
        captured["schema"] = getattr(options, "schema", None)
        return MagicMock()

    with patch("core.auth.create_client", side_effect=fake_create_client):
        from core.auth import get_supabase_client
        get_supabase_client("some.jwt.token")
    # user-JWT client must NOT be pinned to wajagents (chat hits public tables)
    assert captured["schema"] in (None, "public")
