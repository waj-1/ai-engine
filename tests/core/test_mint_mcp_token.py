import jwt
import pytest

from scripts.mint_mcp_token import mint_token

SECRET = "test-jwt-secret"


@pytest.fixture(autouse=True)
def _secret_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)


def test_minted_token_passes_verified_decode():
    from core.auth import validate_token_verified
    tok = mint_token(sub="auth-42", email="admin@company.example", days=1)
    decoded = validate_token_verified(tok)
    assert decoded["sub"] == "auth-42"
    assert decoded["email"] == "admin@company.example"
    assert decoded["token_use"] == "mcp"
    assert decoded["role"] == "authenticated"


def test_expiry_matches_days():
    tok = mint_token(sub="auth-42", days=90)
    decoded = jwt.decode(tok, SECRET, algorithms=["HS256"], audience="authenticated")
    assert 89 * 86400 < decoded["exp"] - decoded["iat"] <= 90 * 86400


def test_missing_secret_raises(monkeypatch):
    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)
    with pytest.raises(SystemExit):
        mint_token(sub="auth-42")
