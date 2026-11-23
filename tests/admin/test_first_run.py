"""First-run setup (`/admin/setup`, admin/services/first_run.py).

What these pin:

1. First run = no completed setup row and no superuser, decided once at boot,
   answered from memory afterwards, closed on any read error.
2. The setup code: printed once per boot in a banner and nowhere else, stored
   as a digest, compared in constant time, locked after five wrong tries,
   expired after its TTL, void once an admin exists.
3. Step 1 creates exactly one admin (also when two submit at once), signs
   them in like the login form, and then never opens again.
4. Step 2 stores and tests a model key through the provider-keys service, or
   is skipped; step 3 creates a Full access key and shows it once.
5. A headless install (an admin made at init) never sees the page, and a
   build with a product plugin is inert.
6. The code, the password and the API key never reach a log line, a URL, a
   cookie or (code, password) any response.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from admin import first_run_handlers as handlers
from admin.services import first_run
from core import api_keys as keys
from core import provider_keys as pk
from tests.api_keys_fake import install as install_api_keys
from tests.first_run_fake import FakeStack
from tests.provider_keys_fake import FakeDB

ME = "00000000-0000-0000-0000-000000000001"  # the conftest's signed-in admin
EMAIL = "owner@corp.test"
PASSWORD = "correct horse battery 42"
CODE_RE = re.compile(r"\b[A-Z2-9]{4}(?:-[A-Z2-9]{4}){3}\b")
KEY_RE = re.compile(r"wae_[A-Za-z0-9_-]{43}")
SECRET = "unit-test-secret-key-0123456789"
MODEL_KEY = "sk-sample-ZYXWVUTSRQPONMLK-7788"


@pytest.fixture
def stack(monkeypatch, client):
    fake = FakeStack()
    fake.next_user_id = ME
    monkeypatch.setattr(first_run.plugins, "has_product", lambda: False)
    monkeypatch.delenv("FIRST_RUN_SETUP", raising=False)
    monkeypatch.setattr(first_run, "_db", lambda: fake)
    monkeypatch.setattr(first_run, "_main", lambda: fake)
    monkeypatch.setattr("admin.audit.record_change", lambda *a, **k: None)
    monkeypatch.setattr("core.auth.sign_in_with_password",
                        lambda email, password: {"access_token": "session-token", "refresh_token": ""})
    first_run.reset_for_tests()
    client.cookies.clear()
    yield fake
    first_run.reset_for_tests()
    client.cookies.clear()


def boot(capsys) -> str:
    """Start the engine's first-run check; return the code from the banner."""
    capsys.readouterr()
    asyncio.run(first_run.on_boot())
    out = capsys.readouterr().out
    found = CODE_RE.findall(out)
    return found[0] if found else ""


def submit(client, code, email=EMAIL, password=PASSWORD, confirm=None, **kwargs):
    return client.post("/admin/setup", data={
        "email": email, "password": password,
        "password_confirm": password if confirm is None else confirm, "code": code,
    }, follow_redirects=False, **kwargs)


# --------------------------------------------------------------------------
# 1. Detection
# --------------------------------------------------------------------------


def test_no_superuser_means_first_run_and_the_entry_pages_lead_to_setup(stack, client, capsys):
    assert boot(capsys)
    assert first_run.state() == "first_run"
    for path in ("/", "/admin", "/admin/", "/admin/login"):
        r = client.get(path, follow_redirects=False)
        assert (r.status_code, r.headers["location"]) == (303, "/admin/setup"), path
    page = client.get("/admin/setup")
    assert page.status_code == 200 and 'name="code"' in page.text
    assert "no default login" in page.text


def test_every_other_admin_route_still_requires_a_login_in_first_run(stack, client, capsys):
    from admin import auth
    from main import app

    boot(capsys)
    saved = app.dependency_overrides.pop(auth.require_superuser)
    try:
        for path in ("/admin/live", "/admin/api-keys", "/admin/model-keys",
                     "/admin/setup/model-key", "/admin/setup/api-key"):
            assert client.get(path, follow_redirects=False).status_code == 401, path
        assert client.post("/admin/setup/api-key", follow_redirects=False).status_code == 401
    finally:
        app.dependency_overrides[auth.require_superuser] = saved


def test_an_existing_superuser_means_no_first_run_no_code_and_no_page(stack, client, capsys):
    """The headless path: init made the admin before the engine started."""
    stack.add_superuser()
    assert boot(capsys) == ""
    assert first_run.state() == "done"
    # Setup is closed for good: recorded, with no code digest.
    assert stack.setup_row["completed_at"] and not stack.setup_row.get("code_hash")
    assert client.get("/admin/login").status_code == 200
    r = client.get("/admin/setup", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/admin/login")
    r = submit(client, "AAAA-AAAA-AAAA-AAAA")
    assert (r.status_code, r.headers["location"]) == (303, "/admin/login")
    assert stack.create_calls == []
    assert client.get("/", follow_redirects=False).status_code == 200


def test_setup_never_reopens_after_completion_even_with_no_admin_left(stack, capsys):
    stack.add_superuser()
    boot(capsys)
    stack.tables["user"].clear()  # every admin removed later
    first_run.reset_for_tests()   # a new engine process
    assert boot(capsys) == ""
    assert first_run.state() == "done"
    assert asyncio.run(first_run.in_first_run()) is False


def test_the_per_request_question_is_answered_from_memory(stack, capsys):
    boot(capsys)
    stack.queries.clear()
    for _ in range(20):
        assert asyncio.run(first_run.in_first_run()) is True
    assert stack.queries == []  # within the re-check interval: no database call

    stack.add_superuser()       # an admin made somewhere else
    first_run._next_check = 0.0
    assert asyncio.run(first_run.in_first_run()) is False
    stack.queries.clear()
    assert asyncio.run(first_run.in_first_run()) is False
    assert stack.queries == [] and first_run.state() == "done"  # cached for good


def test_a_database_error_at_boot_keeps_setup_closed(stack, client, capsys, caplog):
    stack.fail = True
    with caplog.at_level(logging.WARNING):
        assert boot(capsys) == ""
    assert first_run.state() == "unknown"
    assert "setup page stays closed" in caplog.text
    r = client.get("/admin/setup", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/admin/login")
    assert client.get("/admin/login").status_code == 200


def test_no_first_run_check_runs_on_the_event_loop(stack, capsys):
    """supabase-py blocks: every read must happen on a worker thread."""
    seen = []
    real = first_run._is_open_sync

    def spy():
        seen.append(threading.current_thread() is threading.main_thread())
        return real()

    with patch.object(first_run, "_is_open_sync", spy):
        boot(capsys)
        first_run._next_check = 0.0
        asyncio.run(first_run.in_first_run())
    assert seen == [False, False]


# --------------------------------------------------------------------------
# 2. The code
# --------------------------------------------------------------------------


def test_the_code_is_typeable_and_high_entropy():
    codes = {first_run.generate_code() for _ in range(200)}
    assert len(codes) == 200
    for code in codes:
        assert CODE_RE.fullmatch(code)
        assert not set(code) & set("01OIL")
    import math
    assert 16 * math.log2(len(first_run.CODE_ALPHABET)) > 72


def test_the_banner_is_printed_once_per_boot_and_only_on_stdout(stack, capsys, caplog):
    with caplog.at_level(logging.DEBUG):
        capsys.readouterr()
        asyncio.run(first_run.on_boot())
        asyncio.run(first_run.on_boot())  # a second call in the same process
        captured = capsys.readouterr()
    codes = CODE_RE.findall(captured.out)
    assert len(codes) == 1 and "FIRST-RUN SETUP" in captured.out
    assert codes[0] not in captured.err
    assert codes[0] not in caplog.text
    # Only a digest is stored, and it is the printed code's.
    row = stack.setup_row
    assert codes[0] not in str(row) and first_run.normalize_code(codes[0]) not in str(row)
    assert row["code_hash"] == first_run.hash_code(codes[0])
    assert first_run._parse(row["code_expires_at"]) > datetime.now(timezone.utc) + timedelta(hours=23)


def test_each_boot_replaces_the_code(stack, client, capsys):
    old = boot(capsys)
    first_run.reset_for_tests()  # the engine restarted before the code was typed
    new = boot(capsys)
    assert old and new and old != new
    assert submit(client, old).status_code == 403
    assert stack.create_calls == []
    assert submit(client, new).status_code == 303


def test_the_code_is_compared_in_constant_time(stack, client, capsys, monkeypatch):
    code = boot(capsys)
    calls = []
    real = first_run.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(first_run.hmac, "compare_digest", spy)
    assert submit(client, "AAAA-BBBB-CCCC-DDDD").status_code == 403
    assert submit(client, code).status_code == 303
    assert len(calls) == 2
    # Digests of equal length are compared, never the code itself.
    assert all(len(a) == len(b) == 64 for a, b in calls)
    # No digest stored: still one comparison, and never a match.
    calls.clear()
    assert first_run.code_matches(code, None) is False and len(calls) == 1


def test_the_code_is_accepted_without_dashes_and_in_lower_case(stack, client, capsys):
    code = boot(capsys)
    assert submit(client, code.replace("-", " ").lower()).status_code == 303


def test_five_wrong_codes_lock_setup_until_a_restart(stack, client, capsys):
    code = boot(capsys)
    for n in range(1, 5):
        r = submit(client, "AAAA-BBBB-CCCC-DDDD")
        assert r.status_code == 403 and f"{5 - n} tr" in r.text
    r = submit(client, "AAAA-BBBB-CCCC-DDDD")
    assert r.status_code == 429 and "Restart the engine" in r.text
    # Locked: the right code is refused too, and nothing was created.
    r = submit(client, code)
    assert r.status_code == 429 and "locked" in r.text
    assert stack.create_calls == [] and stack.superusers == []
    # The way back: a restart issues a new code and resets the count.
    first_run.reset_for_tests()
    new = boot(capsys)
    assert stack.setup_row["failed_attempts"] == 0
    assert submit(client, new).status_code == 303


def test_a_right_code_with_a_bad_password_does_not_use_up_a_try(stack, client, capsys):
    code = boot(capsys)
    assert submit(client, code, password="short").status_code == 400
    assert stack.setup_row["failed_attempts"] == 0


def test_an_expired_code_is_refused(stack, client, capsys):
    code = boot(capsys)
    stack.setup_row["code_expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    r = submit(client, code)
    assert r.status_code == 410 and "expired" in r.text and "Restart the engine" in r.text
    assert stack.create_calls == []


def test_the_code_is_void_once_an_admin_exists(stack, client, capsys):
    code = boot(capsys)
    assert submit(client, code).status_code == 303
    row = stack.setup_row
    assert row["code_hash"] is None and row["completed_at"] and row["completed_by"] == ME
    again = submit(client, code, email="second@corp.test")
    assert (again.status_code, again.headers["location"]) == (303, "/admin/login")
    assert len(stack.create_calls) == 1 and len(stack.superusers) == 1
    # Also in a fresh process that has not cached anything.
    first_run.reset_for_tests()
    first_run._state = "first_run"
    with pytest.raises(first_run.SetupClosed):
        asyncio.run(first_run.create_admin("third@corp.test", PASSWORD, code))
    assert len(stack.create_calls) == 1


# --------------------------------------------------------------------------
# 3. Step 1: the admin account
# --------------------------------------------------------------------------


def test_step_one_creates_the_admin_and_signs_in_like_the_login_form(stack, client, capsys):
    code = boot(capsys)
    r = submit(client, code)
    assert (r.status_code, r.headers["location"]) == (303, "/admin/setup/model-key")
    # The auth user, confirmed, and the superuser row pointing at it.
    (call,) = stack.create_calls
    assert call == {"email": EMAIL, "password": PASSWORD, "email_confirm": True}
    (row,) = stack.superusers
    assert row["auth_id"] == stack.auth_users[0].id and row["email"] == EMAIL
    assert row["permission"] == {"is_superuser": True} and row["role"] == "admin"
    # The same cookie, with the same attributes, as POST /admin/login sets.
    with patch("core.auth.sign_in_with_password",
               return_value={"access_token": "session-token", "refresh_token": ""}):
        login = client.post("/admin/login", data={"email": EMAIL, "password": PASSWORD},
                            follow_redirects=False)
    assert r.headers["set-cookie"] == login.headers["set-cookie"]
    assert "waj_admin_token=session-token" in r.headers["set-cookie"]
    assert "HttpOnly" in r.headers["set-cookie"] and "SameSite=lax" in r.headers["set-cookie"]
    # And the page is gone.
    assert first_run.state() == "done"
    gone = client.get("/admin/setup", follow_redirects=False)
    assert (gone.status_code, gone.headers["location"]) == (303, "/admin/login")
    assert client.get("/admin/login").status_code == 200


@pytest.mark.parametrize("password", [
    "short", "password123", "1234567890", "aaaaaaaaaaaa", "abcabcabcabc", "abcdefghijkl",
    "owner@corp.test", "ownerowner1", "qwertyuiop", "x" * 73,
])
def test_weak_passwords_are_refused(stack, client, capsys, password):
    code = boot(capsys)
    r = submit(client, code, password=password)
    assert r.status_code == 400 and stack.create_calls == []
    if password != EMAIL:  # the email field is filled in again; a password never is
        assert password not in r.text


def test_other_bad_input_is_refused_without_creating_anything(stack, client, capsys):
    code = boot(capsys)
    assert submit(client, code, confirm="something else 99").status_code == 400
    assert submit(client, code, email="not-an-email").status_code == 400
    assert submit(client, code, password=code).status_code == 400
    assert stack.create_calls == [] and first_run.state() == "first_run"


def test_what_the_auth_service_refuses_is_shown_and_setup_stays_open(stack, client, capsys):
    code = boot(capsys)
    stack.create_error = "Unable to validate email address: invalid format"
    r = submit(client, code)
    assert r.status_code == 400 and "invalid format" in r.text
    assert stack.superusers == [] and first_run.state() == "first_run"
    assert stack.setup_row["claimed_at"] is None  # the claim was released
    stack.create_error = None
    assert submit(client, code).status_code == 303


def test_a_failed_row_write_removes_the_auth_user_it_made(stack, client, capsys):
    code = boot(capsys)
    stack.fail_tables = {"user"}
    # The superuser lookup fails first here, so drive the shared function directly.
    from core.admin_account import AdminAccountError, RestUserRows, ensure_admin_account

    with pytest.raises(AdminAccountError):
        ensure_admin_account(stack, RestUserRows(stack), EMAIL, PASSWORD)
    assert stack.auth_users == [] and len(stack.deleted) == 1
    assert code  # setup is still open
    stack.fail_tables = set()
    assert submit(client, code).status_code == 303


def test_two_people_submitting_at_once_create_one_admin(stack, capsys):
    """Same process: both requests are in flight on one event loop."""
    code = boot(capsys)
    stack.create_delay = 0.05

    async def both():
        return await asyncio.gather(
            first_run.create_admin("first@corp.test", PASSWORD, code),
            first_run.create_admin("second@corp.test", PASSWORD, code),
            return_exceptions=True)

    results = asyncio.run(both())
    winners = [r for r in results if isinstance(r, dict)]
    losers = [r for r in results if isinstance(r, Exception)]
    assert len(winners) == 1 and len(losers) == 1
    assert isinstance(losers[0], (first_run.SetupClosed, first_run.SetupError))
    assert len(stack.create_calls) == 1 and len(stack.superusers) == 1
    assert len(stack.auth_users) == 1


def test_two_engine_processes_submitting_at_once_create_one_admin(stack, capsys):
    """Two processes share no lock, only the database: the claim decides."""
    code = boot(capsys)
    stack.create_delay = 0.1
    start = threading.Barrier(2)
    results = []

    def one(email):
        start.wait()
        try:
            results.append(first_run._create_admin_sync(email, PASSWORD, code))
        except Exception as exc:  # noqa: BLE001
            results.append(exc)

    threads = [threading.Thread(target=one, args=(f"p{i}@corp.test",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(stack.create_calls) == 1 and len(stack.superusers) == 1
    losers = [r for r in results if isinstance(r, Exception)]
    assert len(losers) == 1 and isinstance(losers[0], (first_run.SetupError, first_run.SetupClosed))


def test_a_claim_left_by_a_dead_process_does_not_block_setup(stack, client, capsys):
    code = boot(capsys)
    stack.setup_row["claimed_at"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    assert submit(client, code).status_code == 303
    # A live claim does block.
    first_run.reset_for_tests()
    stack.tables["user"].clear()
    stack.tables["engine_setup"].clear()
    code = boot(capsys)
    stack.setup_row["claimed_at"] = datetime.now(timezone.utc).isoformat()
    assert submit(client, code).status_code == 409


# --------------------------------------------------------------------------
# Request forgery
# --------------------------------------------------------------------------


@pytest.mark.parametrize("headers", [
    {"sec-fetch-site": "cross-site"},
    {"sec-fetch-site": "same-site"},
    {"origin": "https://evil.example"},
    {"origin": "null"},
])
def test_a_cross_site_post_is_refused_and_uses_no_try(stack, client, capsys, headers):
    code = boot(capsys)
    r = submit(client, "AAAA-BBBB-CCCC-DDDD", headers=headers)
    assert r.status_code == 403
    assert stack.setup_row["failed_attempts"] == 0
    assert submit(client, code, headers=headers).status_code == 403
    assert stack.create_calls == []
    # The engine's own page (and a client that is not a browser) is accepted.
    assert submit(client, code, headers={"sec-fetch-site": "same-origin",
                                         "origin": "http://testserver"}).status_code == 303


def test_the_signed_in_steps_refuse_cross_site_posts_too(stack, client, capsys):
    submit(client, boot(capsys))
    for path in ("/admin/setup/model-key", "/admin/setup/api-key"):
        r = client.post(path, data={"provider": "openai", "value": MODEL_KEY},
                        headers={"sec-fetch-site": "cross-site"}, follow_redirects=False)
        assert r.status_code == 403, path


# --------------------------------------------------------------------------
# 4. Steps 2 and 3
# --------------------------------------------------------------------------


@pytest.fixture
def model_db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setenv("SECRET_KEY", SECRET)
    monkeypatch.setattr("core.auth.get_service_client", lambda: fake)
    monkeypatch.setattr("core.auth.get_service_client_for_env", lambda env: fake)
    pk.invalidate()
    with patch("admin.router.client_for", return_value=fake):
        yield fake
    pk.invalidate()


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.text = status, body or {}, ""

    def json(self):
        return self._body


def test_step_two_stores_the_key_encrypted_and_tests_it(stack, client, capsys, model_db, monkeypatch):
    from admin.services import provider_keys as svc

    submit(client, boot(capsys))
    page = client.get("/admin/setup/model-key")
    assert page.status_code == 200 and 'name="provider"' in page.text
    assert "I will use a local model" in page.text and "Ollama" in page.text
    assert "Skip for now" in page.text

    seen = []
    monkeypatch.setattr(svc, "_http_get", lambda url, headers: (seen.append((url, headers)), _Resp(200))[1])
    r = client.post("/admin/setup/model-key", data={"provider": "openai", "value": MODEL_KEY})
    assert r.status_code == 200 and "is stored and works" in r.text and "Continue" in r.text
    assert MODEL_KEY not in r.text
    (row,) = model_db.tables["provider_keys"]
    assert row["provider"] == "openai" and MODEL_KEY not in str(row)
    assert pk.decrypt(row["ciphertext"]) == MODEL_KEY
    assert seen and seen[0][1]["Authorization"] == f"Bearer {MODEL_KEY}"


def test_step_two_says_when_the_provider_refuses_the_key(stack, client, capsys, model_db, monkeypatch):
    from admin.services import provider_keys as svc

    submit(client, boot(capsys))
    monkeypatch.setattr(svc, "_http_get", lambda url, headers: _Resp(
        401, {"error": {"message": f"Incorrect API key provided: {MODEL_KEY}"}}))
    r = client.post("/admin/setup/model-key", data={"provider": "openai", "value": MODEL_KEY})
    assert r.status_code == 200 and "the test failed" in r.text and "Skip for now" in r.text
    assert MODEL_KEY not in r.text


def test_step_two_without_a_secret_key_explains_and_lets_you_skip(stack, client, capsys, model_db, monkeypatch):
    monkeypatch.delenv("SECRET_KEY")
    submit(client, boot(capsys))
    page = client.get("/admin/setup/model-key")
    assert "cannot be stored on this server yet" in page.text and "SECRET_KEY is not set" in page.text
    assert 'id="setup-model-key-form"' not in page.text and "Skip for now" in page.text
    r = client.post("/admin/setup/model-key", data={"provider": "openai", "value": MODEL_KEY})
    assert r.status_code == 400 and MODEL_KEY not in r.text
    assert model_db.tables.get("provider_keys", []) == []


def test_step_two_refuses_an_unknown_provider(stack, client, capsys, model_db):
    submit(client, boot(capsys))
    r = client.post("/admin/setup/model-key", data={"provider": "nope", "value": MODEL_KEY})
    assert r.status_code == 400 and "Unknown provider" in r.text


def test_step_three_creates_a_full_access_key_and_shows_it_once(stack, client, capsys, monkeypatch):
    store = install_api_keys(monkeypatch)
    monkeypatch.setattr(handlers, "_first_agent_slug", lambda: None)
    submit(client, boot(capsys))
    page = client.get("/admin/setup/api-key")
    assert page.status_code == 200 and "Create my API key" in page.text
    assert KEY_RE.findall(page.text) == [] and store.rows == []

    r = client.post("/admin/setup/api-key", follow_redirects=False)
    assert r.status_code == 200 and "location" not in r.headers and "set-cookie" not in r.headers
    assert r.headers.get("cache-control") == "no-store"
    (key,) = set(KEY_RE.findall(r.text))
    assert keys.verify_key(key)["scopes"] == ["chat", "admin"]
    assert store.rows[0]["created_by"] == ME and key not in str(store.rows[0])
    text = " ".join(r.text.split())
    assert "You will not see it again" in text and 'data-copy="setup-api-key-value"' in r.text
    # Ready-to-paste examples: a curl chat call and the MCP client JSON.
    assert "curl -N http://testserver/v1/agents/YOUR-AGENT/chat" in r.text
    assert f"Authorization: Bearer {key}" in r.text
    assert '"url": "http://testserver/mcp"' in r.text
    assert 'href="/admin/"' in r.text and "Go to the portal" in r.text

    # The walkthrough is over: no second key, and its pages lead to the portal.
    for call in (lambda: client.post("/admin/setup/api-key", follow_redirects=False),
                 lambda: client.get("/admin/setup/api-key", follow_redirects=False),
                 lambda: client.get("/admin/setup/model-key", follow_redirects=False)):
        again = call()
        assert (again.status_code, again.headers["location"]) == (303, "/admin/")
    assert len(store.rows) == 1 and stack.setup_row["finished_at"]


def test_the_example_uses_a_published_agent_when_there_is_one(stack, client, capsys, monkeypatch):
    install_api_keys(monkeypatch)
    monkeypatch.setattr(handlers, "_first_agent_slug", lambda: "support-desk")
    submit(client, boot(capsys))
    r = client.post("/admin/setup/api-key")
    assert "/v1/agents/support-desk/chat" in r.text and "YOUR-AGENT" not in r.text


def test_the_walkthrough_is_only_for_the_admin_who_set_up(stack, client, capsys, monkeypatch):
    """A headless install, or any other admin, never sees steps 2 and 3."""
    store = install_api_keys(monkeypatch)
    stack.add_superuser()
    boot(capsys)
    for method, path in (("get", "/admin/setup/model-key"), ("get", "/admin/setup/api-key"),
                         ("post", "/admin/setup/api-key"), ("post", "/admin/setup/model-key")):
        r = getattr(client, method)(path, follow_redirects=False)
        assert (r.status_code, r.headers["location"]) == (303, "/admin/"), path
    assert store.rows == []
    # Set up by someone else: this signed-in admin (ME) is not them.
    stack.setup_row.update({"completed_by": "00000000-0000-0000-0000-0000000000ff", "finished_at": None})
    assert client.get("/admin/setup/api-key", follow_redirects=False).status_code == 303


# --------------------------------------------------------------------------
# 5. A build with a product plugin, and the off switch
# --------------------------------------------------------------------------


def test_a_product_build_is_inert(stack, client, capsys, monkeypatch, caplog):
    monkeypatch.setattr(first_run.plugins, "has_product", lambda: True)
    with caplog.at_level(logging.DEBUG):
        assert boot(capsys) == ""
    assert first_run.state() == "off" and first_run.enabled() is False
    assert stack.queries == [] and "first run" not in caplog.text  # no read, no code, no log
    assert client.get("/admin/login").status_code == 200
    assert client.get("/", follow_redirects=False).status_code == 200
    r = client.get("/admin/setup", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/admin/login")
    r = submit(client, "AAAA-BBBB-CCCC-DDDD")
    assert (r.status_code, r.headers["location"]) == (303, "/admin/login")
    with pytest.raises(first_run.SetupClosed):
        asyncio.run(first_run.create_admin(EMAIL, PASSWORD, "AAAA-BBBB-CCCC-DDDD"))
    assert asyncio.run(first_run.walkthrough_open(ME)) is False
    assert stack.queries == [] and stack.create_calls == []


def test_a_product_build_stays_inert_even_if_its_state_said_first_run(stack, client, monkeypatch):
    """Belt and braces: the build check is asked on every path, not only at boot."""
    monkeypatch.setattr(first_run.plugins, "has_product", lambda: True)
    first_run._state = "first_run"
    assert asyncio.run(first_run.in_first_run()) is False
    assert client.get("/admin/login").status_code == 200
    with pytest.raises(first_run.SetupClosed):
        asyncio.run(first_run.create_admin(EMAIL, PASSWORD, "AAAA-BBBB-CCCC-DDDD"))
    assert stack.queries == []


def test_this_repository_s_own_build_is_what_decides(monkeypatch):
    """No patching: with the real plugin list, the feature is on exactly when
    the build has no product plugin."""
    from core import plugins

    monkeypatch.delenv("FIRST_RUN_SETUP", raising=False)
    assert first_run.enabled() is (not plugins.has_product())


@pytest.mark.parametrize("value", ["0", "false", "off", "no"])
def test_the_setting_turns_it_off_in_a_build_without_a_product(stack, capsys, monkeypatch, value):
    monkeypatch.setenv("FIRST_RUN_SETUP", value)
    assert boot(capsys) == "" and first_run.state() == "off" and stack.queries == []


# --------------------------------------------------------------------------
# 6. Nothing leaks
# --------------------------------------------------------------------------


def test_nothing_leaks_the_code_the_password_or_a_key(stack, client, capsys, caplog, monkeypatch, model_db):
    from admin.services import provider_keys as svc

    api_store = install_api_keys(monkeypatch)
    monkeypatch.setattr(handlers, "_first_agent_slug", lambda: None)
    monkeypatch.setattr(svc, "_http_get", lambda url, headers: _Resp(200))
    responses = []
    with caplog.at_level(logging.DEBUG):
        code = boot(capsys)
        responses.append(client.get("/admin/setup"))
        responses.append(submit(client, "AAAA-BBBB-CCCC-DDDD"))            # wrong code
        responses.append(submit(client, code, password="password123"))     # weak password
        stack.create_error = "Password should be different"
        responses.append(submit(client, code))                             # refused by the auth service
        stack.create_error = None
        responses.append(submit(client, code))                             # created
        responses.append(client.get("/admin/setup/model-key"))
        responses.append(client.post("/admin/setup/model-key",
                                     data={"provider": "openai", "value": MODEL_KEY}))
        final = client.post("/admin/setup/api-key")
        out = capsys.readouterr()
    (api_key,) = set(KEY_RE.findall(final.text))
    logs = caplog.text + out.out + out.err
    for secret in (code, first_run.normalize_code(code), PASSWORD, MODEL_KEY, api_key,
                   stack.auth_users[0].password, keys.hash_key(api_key)):
        assert secret not in logs
    for r in responses:
        blob = r.text + str(dict(r.headers)) + str(r.url)
        for secret in (code, first_run.normalize_code(code), first_run.hash_code(code),
                       PASSWORD, "password123", MODEL_KEY):
            assert secret not in blob, r.url
    # The API key is in exactly one response body and in no header or URL.
    assert api_key not in str(dict(final.headers)) + str(final.url)
    assert api_key not in client.get("/admin/api-keys").text
    assert api_key not in str(api_store.rows) and api_key not in str(stack.tables)
    # Nothing secret was stored in the setup row either.
    assert code not in str(stack.tables) and PASSWORD not in str(stack.tables["engine_setup"])


def test_the_setup_form_never_goes_in_a_url(stack, client, capsys):
    boot(capsys)
    page = client.get("/admin/setup").text
    assert 'method="post" action="/admin/setup"' in page
    assert 'name="referrer" content="no-referrer"' in page
    # A code sent in the query string is ignored, not accepted.
    r = client.post("/admin/setup?code=AAAA-BBBB-CCCC-DDDD", data={
        "email": EMAIL, "password": PASSWORD, "password_confirm": PASSWORD}, follow_redirects=False)
    assert r.status_code in (403, 410) and stack.create_calls == []


def test_setup_posts_are_rate_limited_like_the_login(stack):
    """`/admin/setup` is unauthenticated: the per-IP limiter must cover it."""
    import inspect

    import main

    source = inspect.getsource(main.security_and_rate_limit_middleware)
    assert '"/admin/login", "/admin/logout", "/admin/setup"' in source
