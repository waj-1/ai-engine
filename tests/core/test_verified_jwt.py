"""validate_token_verified: signature-checked JWT decode for admin/MCP auth.

Regression suite for the 2026-07-03 finding that admin auth decoded JWTs
with verify_signature=False (forged sub -> superuser bypass)."""
import time

import jwt
import pytest

from core.auth import TokenValidationError, validate_token_verified

SECRET = "test-jwt-secret"


def _mint(secret=SECRET, *, sub="auth-1", aud="authenticated", exp_delta=3600, **extra):
    payload = {"sub": sub, "aud": aud, "exp": int(time.time()) + exp_delta,
               "iat": int(time.time()), **extra}
    return jwt.encode(payload, secret, algorithm="HS256")


@pytest.fixture(autouse=True)
def _secret_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)


def test_valid_token_decodes():
    decoded = validate_token_verified(_mint(email="a@company.example"))
    assert decoded["sub"] == "auth-1"
    assert decoded["email"] == "a@company.example"


def test_forged_signature_rejected():
    with pytest.raises(TokenValidationError):
        validate_token_verified(_mint(secret="wrong-secret"))


def test_unsigned_alg_none_rejected():
    header_payload = jwt.encode({"sub": "auth-1", "aud": "authenticated",
                                 "exp": int(time.time()) + 3600},
                                key=None, algorithm="none")
    with pytest.raises(TokenValidationError):
        validate_token_verified(header_payload)


def test_expired_rejected():
    with pytest.raises(TokenValidationError, match="expired"):
        validate_token_verified(_mint(exp_delta=-10))


def test_wrong_audience_rejected():
    with pytest.raises(TokenValidationError):
        validate_token_verified(_mint(aud="anon"))


def test_missing_sub_rejected():
    tok = jwt.encode({"aud": "authenticated", "exp": int(time.time()) + 3600},
                     SECRET, algorithm="HS256")
    with pytest.raises(TokenValidationError, match="sub"):
        validate_token_verified(tok)


def test_missing_exp_rejected():
    tok = jwt.encode({"sub": "auth-1", "aud": "authenticated",
                      "iat": int(time.time())}, SECRET, algorithm="HS256")
    with pytest.raises(TokenValidationError):
        validate_token_verified(tok)


def test_missing_secret_fails_closed(monkeypatch):
    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)
    with pytest.raises(TokenValidationError, match="SUPABASE_JWT_SECRET"):
        validate_token_verified(_mint())


# ---------------------------------------------------------------------------
# ES256 via the main project's JWKS (newer Supabase CLI stacks sign this way)
# ---------------------------------------------------------------------------
# The JWKS fetch is faked at urlopen, so these never touch the
# network; `fetches` counts how often the engine went to fetch the keys.
import base64
import hashlib
import hmac
import io
import json
import urllib.error

import jwt.jwks_client
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

import core.auth as auth_mod

KID = "test-kid-1"
_EC_KEY = ec.generate_private_key(ec.SECP256R1())
_OTHER_EC_KEY = ec.generate_private_key(ec.SECP256R1())


def _jwk(private_key, kid=KID):
    jwk = json.loads(ECAlgorithm.to_jwk(private_key.public_key()))
    return {**jwk, "kid": kid, "alg": "ES256", "use": "sig"}


def _mint_es256(private_key=_EC_KEY, *, kid=KID, sub="auth-1", aud="authenticated", exp_delta=3600):
    payload = {"sub": sub, "aud": aud, "exp": int(time.time()) + exp_delta}
    headers = {"kid": kid} if kid else {}
    return jwt.encode(payload, private_key, algorithm="ES256", headers=headers)


@pytest.fixture
def jwks(monkeypatch):
    """Serve a fake JWKS; returns a handle to change the keys / count fetches.

    Faked at urlopen (not PyJWKClient.fetch_data) because fetch_data is also
    what fills PyJWKClient's cache — replacing it would hide caching bugs."""
    state = {"keys": [_jwk(_EC_KEY)], "fetches": 0, "fail": False, "urls": [], "gate": None}

    def fake_urlopen(request, *args, **kwargs):
        state["fetches"] += 1
        state["urls"].append(getattr(request, "full_url", request))
        keys = state["keys"]  # the keys as they were when this fetch started
        if state["gate"]:
            state["gate"]()
        if state["fail"]:
            raise urllib.error.URLError("JWKS unreachable")
        return io.BytesIO(json.dumps({"keys": keys}).encode())

    monkeypatch.setenv("SUPABASE_URL", "http://main.test")
    monkeypatch.delenv("SUPABASE_JWKS_URL", raising=False)
    monkeypatch.delenv("SUPABASE_JWT_ALGORITHMS", raising=False)
    monkeypatch.setattr(jwt.jwks_client.urllib.request, "urlopen", fake_urlopen)
    auth_mod._reset_jwks_cache()
    yield state
    auth_mod._reset_jwks_cache()


def test_es256_valid_token_decodes(jwks):
    assert validate_token_verified(_mint_es256())["sub"] == "auth-1"


def test_es256_keys_are_cached_between_requests(jwks):
    validate_token_verified(_mint_es256())
    validate_token_verified(_mint_es256())
    assert jwks["fetches"] == 1


def test_es256_jwks_url_derived_from_supabase_url(jwks):
    validate_token_verified(_mint_es256())
    assert jwks["urls"] == ["http://main.test/auth/v1/.well-known/jwks.json"]


def test_es256_signed_with_wrong_key_rejected(jwks):
    with pytest.raises(TokenValidationError):
        validate_token_verified(_mint_es256(_OTHER_EC_KEY))


def test_es256_expired_rejected(jwks):
    with pytest.raises(TokenValidationError, match="expired"):
        validate_token_verified(_mint_es256(exp_delta=-10))


def test_es256_wrong_audience_rejected(jwks):
    with pytest.raises(TokenValidationError):
        validate_token_verified(_mint_es256(aud="anon"))


def test_es256_missing_kid_rejected(jwks):
    with pytest.raises(TokenValidationError, match="kid"):
        validate_token_verified(_mint_es256(kid=None))


def test_es256_unknown_kid_rejected_after_one_refresh(jwks):
    with pytest.raises(TokenValidationError, match="unknown key"):
        validate_token_verified(_mint_es256(kid="not-in-jwks"))
    assert jwks["fetches"] == 2  # the cached fetch + one forced refresh


def test_es256_rotated_key_picked_up_by_refresh(jwks):
    validate_token_verified(_mint_es256())  # caches the old key set
    jwks["keys"] = [_jwk(_OTHER_EC_KEY, kid="rotated")]
    assert validate_token_verified(_mint_es256(_OTHER_EC_KEY, kid="rotated"))["sub"] == "auth-1"


def test_es256_unknown_kid_refresh_is_rate_limited(jwks):
    for i in range(5):
        with pytest.raises(TokenValidationError):
            validate_token_verified(_mint_es256(kid=f"random-{i}"))
    assert jwks["fetches"] == 2  # not one fetch per forged token


def test_es256_jwks_unreachable_fails_closed(jwks):
    jwks["fail"] = True
    with pytest.raises(TokenValidationError, match="signing keys"):
        validate_token_verified(_mint_es256())


def test_es256_without_jwks_source_fails_closed(jwks, monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    with pytest.raises(TokenValidationError, match="JWKS"):
        validate_token_verified(_mint_es256())


def test_es256_accepted_without_jwt_secret(jwks, monkeypatch):
    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)
    assert validate_token_verified(_mint_es256())["sub"] == "auth-1"


def test_algorithm_confusion_hs256_signed_with_public_key_rejected(jwks):
    """An HS256 token 'signed' with the public key must not verify: the HS256
    branch only ever checks against SUPABASE_JWT_SECRET.

    Two layers hold this today — our per-branch key choice AND PyJWT's own
    refusal to HMAC with an asymmetric key — so this test does NOT fail if
    only our layer is removed (checked). It pins the outcome, not which
    layer produces it."""
    public_pem = _EC_KEY.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    payload = {"sub": "attacker", "aud": "authenticated", "exp": int(time.time()) + 3600}
    # Built by hand: PyJWT itself refuses to HMAC-sign with a PEM key.
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=")
    signing_input = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode()) \
        + b"." + b64(json.dumps(payload).encode())
    signature = hmac.new(public_pem, signing_input, hashlib.sha256).digest()
    forged = (signing_input + b"." + b64(signature)).decode()
    with pytest.raises(TokenValidationError):
        validate_token_verified(forged)


def test_disallowed_algorithm_rejected(jwks, monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_ALGORITHMS", "ES256")
    with pytest.raises(TokenValidationError, match="not allowed"):
        validate_token_verified(_mint())  # a valid HS256 token


def test_hs256_still_accepted_by_default(jwks):
    assert validate_token_verified(_mint())["sub"] == "auth-1"
    assert jwks["fetches"] == 0  # HS256 never touches the JWKS


def test_warm_jwks_cache_fetches_once_and_never_raises(jwks):
    auth_mod.warm_jwks_cache()
    validate_token_verified(_mint_es256())
    assert jwks["fetches"] == 1
    jwks["fail"] = True
    auth_mod._reset_jwks_cache()
    auth_mod.warm_jwks_cache()  # must not raise


# ---------------------------------------------------------------------------
# SUPABASE_JWT_ALGORITHMS: taken literally, or verified auth fails closed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["ES265", "RS256", "ES256,RS256", "none", "HS256,"])
def test_unsupported_algorithm_setting_fails_closed(jwks, monkeypatch, value):
    """A typo meant to turn HS256 OFF must not quietly turn both back ON."""
    monkeypatch.setenv("SUPABASE_JWT_ALGORITHMS", value)
    if value == "HS256,":   # a trailing comma is harmless, not an unknown value
        assert validate_token_verified(_mint())["sub"] == "auth-1"
        return
    for token in (_mint(), _mint_es256()):
        with pytest.raises(auth_mod.AuthConfigError, match="SUPABASE_JWT_ALGORITHMS"):
            validate_token_verified(token)


def test_algorithm_setting_is_case_and_space_insensitive(jwks, monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_ALGORITHMS", " es256 ")
    assert validate_token_verified(_mint_es256())["sub"] == "auth-1"
    with pytest.raises(TokenValidationError, match="not allowed"):
        validate_token_verified(_mint())


def test_empty_algorithm_setting_means_the_defaults(jwks, monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_ALGORITHMS", "  ")
    assert validate_token_verified(_mint())["sub"] == "auth-1"
    assert validate_token_verified(_mint_es256())["sub"] == "auth-1"


def test_bad_algorithm_setting_skips_the_refresh(jwks, monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_ALGORITHMS", "ES265")
    assert auth_mod.refresh_jwks_cache() is False
    assert jwks["fetches"] == 0


# ---------------------------------------------------------------------------
# Background refresh: the request path reads a warm cache
# ---------------------------------------------------------------------------

def test_refresh_then_requests_do_not_fetch(jwks):
    assert auth_mod.refresh_jwks_cache() is True
    for _ in range(3):
        validate_token_verified(_mint_es256())
    assert jwks["fetches"] == 1   # the refresh only


def test_failed_refresh_keeps_the_last_good_keys(jwks):
    """PyJWKClient clears its cache on a failed fetch; ours must not."""
    auth_mod.refresh_jwks_cache()
    jwks["fail"] = True
    assert auth_mod.refresh_jwks_cache() is False
    assert validate_token_verified(_mint_es256())["sub"] == "auth-1"


def test_refresh_picks_up_a_rotated_key_without_a_request_fetch(jwks):
    auth_mod.refresh_jwks_cache()
    jwks["keys"] = [_jwk(_OTHER_EC_KEY, kid="rotated")]
    auth_mod.refresh_jwks_cache()
    fetches = jwks["fetches"]
    assert validate_token_verified(_mint_es256(_OTHER_EC_KEY, kid="rotated"))["sub"] == "auth-1"
    assert jwks["fetches"] == fetches


def test_keys_older_than_max_age_are_refetched(jwks, monkeypatch):
    """If the refresher dies, a revoked key can't stay trusted forever."""
    auth_mod.refresh_jwks_cache()
    real = auth_mod.time.monotonic
    monkeypatch.setattr(auth_mod.time, "monotonic", lambda: real() + auth_mod._JWKS_MAX_AGE_SEC + 1)
    validate_token_verified(_mint_es256())
    assert jwks["fetches"] == 2


def test_refresh_without_es256_or_url_does_nothing(jwks, monkeypatch):
    monkeypatch.setenv("SUPABASE_JWT_ALGORITHMS", "HS256")
    assert auth_mod.refresh_jwks_cache() is False
    monkeypatch.delenv("SUPABASE_JWT_ALGORITHMS")
    monkeypatch.delenv("SUPABASE_URL")
    assert auth_mod.refresh_jwks_cache() is False
    assert jwks["fetches"] == 0


def _clock(monkeypatch):
    """Controllable monotonic clock; returns a function that advances it."""
    now = [auth_mod.time.monotonic()]
    monkeypatch.setattr(auth_mod.time, "monotonic", lambda: now[0])

    def advance(sec):
        now[0] += sec
    return advance


def test_failed_cold_fetch_is_not_retried_by_every_request(jwks, monkeypatch):
    """A JWKS outage must not turn every forged ES256 token into a 3s fetch."""
    advance = _clock(monkeypatch)
    jwks["fail"] = True
    for _ in range(5):
        with pytest.raises(TokenValidationError):
            validate_token_verified(_mint_es256())
    assert jwks["fetches"] == 1
    advance(auth_mod._JWKS_FAILED_FETCH_RETRY_SEC + 1)
    jwks["fail"] = False
    assert validate_token_verified(_mint_es256())["sub"] == "auth-1"  # recovers
    assert jwks["fetches"] == 2


def test_failed_fetch_of_too_old_keys_is_rate_limited_and_fails_closed(jwks, monkeypatch):
    advance = _clock(monkeypatch)
    auth_mod.refresh_jwks_cache()
    advance(auth_mod._JWKS_MAX_AGE_SEC + 1)
    jwks["fail"] = True
    for _ in range(5):
        with pytest.raises(TokenValidationError):  # old keys are not trusted
            validate_token_verified(_mint_es256())
    assert jwks["fetches"] == 2   # the refresh + one failed refetch


def test_a_successful_refresh_clears_the_failure_window(jwks, monkeypatch):
    _clock(monkeypatch)
    jwks["fail"] = True
    with pytest.raises(TokenValidationError):
        validate_token_verified(_mint_es256())
    jwks["fail"] = False
    assert auth_mod.refresh_jwks_cache() is True
    assert validate_token_verified(_mint_es256())["sub"] == "auth-1"


def test_jwks_fetches_run_one_at_a_time_so_an_older_one_cannot_win(jwks):
    """The refresher and a request-triggered fetch used to run side by side;
    the one that STARTED first could finish last and store the older keys."""
    import threading

    auth_mod.refresh_jwks_cache()                 # warm: the original key
    first_fetch_started = threading.Event()
    release_first_fetch = threading.Event()

    def gate():
        if not first_fetch_started.is_set():
            first_fetch_started.set()
            release_first_fetch.wait(5)
    jwks["gate"] = gate

    slow = threading.Thread(target=auth_mod.refresh_jwks_cache)   # fetches the OLD keys
    slow.start()
    assert first_fetch_started.wait(5)
    jwks["keys"] = [_jwk(_OTHER_EC_KEY, kid="rotated")]           # rotation happens
    newer = threading.Thread(target=auth_mod.refresh_jwks_cache)
    newer.start()
    newer.join(0.3)
    assert newer.is_alive() and jwks["fetches"] == 2  # waits; hasn't fetched yet
    # A warm-cache request does not wait behind the slow fetch.
    assert validate_token_verified(_mint_es256())["sub"] == "auth-1"
    release_first_fetch.set()
    slow.join(5)
    newer.join(5)
    assert jwks["fetches"] == 3
    # The newer fetch is the one kept: the rotated key verifies with no fetch.
    assert validate_token_verified(_mint_es256(_OTHER_EC_KEY, kid="rotated"))["sub"] == "auth-1"
    assert jwks["fetches"] == 3


# ---------------------------------------------------------------------------
# Async handlers never run the (possibly fetching) check on the event loop
# ---------------------------------------------------------------------------

_BLOCKING_AUTH_CALLS = {"authenticate_verified", "validate_token_verified",
                        "resolve_superuser", "_request_is_operator"}


@pytest.mark.parametrize("module_path", ["main.py", "endpoints/ai_sessions.py", "endpoints/voice_live.py"])
def test_no_async_function_calls_verified_auth_inline(module_path):
    """Structural pin: inside every `async def`, a verified-auth call only
    appears as an argument to `asyncio.to_thread(...)`, never called directly.
    (A timing test can't prove this: TestClient runs each request on its own
    portal thread, so a blocking call there passes anyway.)"""
    import ast
    from pathlib import Path

    tree = ast.parse((Path(__file__).resolve().parents[2] / module_path).read_text())
    offenders = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name in _BLOCKING_AUTH_CALLS:
                    offenders.append(f"{fn.name}:{node.lineno} calls {name}() inline")
    assert not offenders, offenders
