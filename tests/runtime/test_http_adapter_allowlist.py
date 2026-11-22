"""HTTP_TOOL_ALLOW_HOSTS: deliberate loopback exceptions for the SSRF guard.

The guard hard-blocks loopback (right for prod: cloud-metadata/localhost
exfiltration). A partner demo mock runs at 127.0.0.1:8080 in local dev, so
its http tools were dead locally. The allowlist is exact-host:port,
operator-set, and NEVER upgrades a target to 'public' — auth_env_var
secrets stay unforwarded even to allowlisted hosts."""
import runtime.tools._http_adapter as ha


def test_loopback_is_blocked_by_default(monkeypatch):
    monkeypatch.delenv("HTTP_TOOL_ALLOW_HOSTS", raising=False)
    assert ha._classify_endpoint("http://127.0.0.1:8080/api/iban") == "blocked"


def test_allowlisted_loopback_becomes_private_not_public(monkeypatch):
    monkeypatch.setenv("HTTP_TOOL_ALLOW_HOSTS", "127.0.0.1:8080")
    assert ha._classify_endpoint("http://127.0.0.1:8080/api/iban") == "private"


def test_allowlist_is_exact_port(monkeypatch):
    monkeypatch.setenv("HTTP_TOOL_ALLOW_HOSTS", "127.0.0.1:8080")
    assert ha._classify_endpoint("http://127.0.0.1:9999/x") == "blocked"


def test_allowlist_does_not_touch_metadata_endpoint(monkeypatch):
    monkeypatch.setenv("HTTP_TOOL_ALLOW_HOSTS", "127.0.0.1:8080")
    assert ha._classify_endpoint("http://169.254.169.254/latest") == "blocked"


def test_multiple_entries_and_whitespace(monkeypatch):
    monkeypatch.setenv("HTTP_TOOL_ALLOW_HOSTS", " localhost:8080 , 127.0.0.1:8080 ")
    assert ha._classify_endpoint("http://localhost:8080/api/tickets") == "private"


def test_secret_is_not_forwarded_to_allowlisted_host(monkeypatch):
    # classification 'private' already implies no auth forwarding in _call;
    # pin the classification so a future refactor can't quietly return 'public'.
    monkeypatch.setenv("HTTP_TOOL_ALLOW_HOSTS", "127.0.0.1:8080")
    assert ha._classify_endpoint("http://127.0.0.1:8080/x") != "public"


def test_malformed_port_does_not_raise(monkeypatch):
    # urlparse().port raises ValueError for non-integer port text (e.g. a
    # stray ".evil.com" glued onto the port). _classify_endpoint must not
    # propagate that — it's on a "never raises for input-shaped problems"
    # path (see admin.tool_handlers.run_tool_dry_run, which calls this with
    # no guard). Env unset: still cleanly "blocked", same as pre-allowlist.
    monkeypatch.delenv("HTTP_TOOL_ALLOW_HOSTS", raising=False)
    assert ha._classify_endpoint("http://127.0.0.1:8080.evil.com/") == "blocked"
