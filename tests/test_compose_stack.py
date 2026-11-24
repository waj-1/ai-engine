"""`docker compose up` at the repo root: the setup step and the compose file.

The stack itself is exercised by running it (docs/open-source/LOCAL_TEST_REPORT.md);
these tests pin what must not regress silently: where secrets live, that a
second run keeps them, and that nothing is published beyond this machine.
"""
import importlib.util
import os
import shlex
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "deploy" / "compose" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _compose() -> dict:
    """The compose file: at the root in the public tree, in the scaffold here."""
    for path in (ROOT / "docker-compose.yml", ROOT / "docs/open-source/scaffold/docker-compose.yml"):
        if path.exists():
            return yaml.safe_load(path.read_text())
    pytest.fail("no root docker-compose.yml")


def _env(text: str) -> dict:
    return {k: shlex.split(v)[0] if v else "" for k, v in
            (line.split("=", 1) for line in text.splitlines() if line)}


@pytest.fixture
def setup_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGINE_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    return _load("setup")


def test_setup_writes_the_three_files_with_one_set_of_secrets(setup_mod, tmp_path, capsys):
    assert setup_mod.main() == 0
    stack = _env((tmp_path / "stack.env").read_text())
    engine = _env((tmp_path / "engine.env").read_text())
    conf = (tmp_path / "postgrest.conf").read_text()
    # One JWT secret signs everything; the engine verifies with the same one.
    assert stack["JWT_SECRET"] == engine["SUPABASE_JWT_SECRET"] == stack["GOTRUE_JWT_SECRET"]
    assert f'jwt-secret = "{stack["JWT_SECRET"]}"' in conf
    assert stack["POSTGRES_PASSWORD"] in conf and stack["POSTGRES_PASSWORD"] in engine["AI_DATABASE_URL"]
    # The engine reaches the stack by service name, not loopback.
    assert engine["SUPABASE_URL"] == engine["AI_SUPABASE_URL"] == "http://gateway:8000"
    assert "@db:5432/" in engine["AI_DATABASE_URL"]
    assert engine["ENABLE_GENERIC_AGENTS"] == "1" and engine["ENABLE_ADMIN_MCP"] == "1"
    # No admin was chosen, so none is invented: no login in the files or the
    # output, and the output says where the account is created.
    out = capsys.readouterr().out
    assert "ADMIN_EMAIL" not in engine and "ADMIN_PASSWORD" not in engine
    everything = out + "".join(p.read_text() for p in tmp_path.iterdir())
    assert "admin@engine.local" not in everything and "admin login" not in out
    assert "first-run page" in out and "docker compose logs engine" in out


def test_a_second_run_keeps_the_install(setup_mod, tmp_path):
    setup_mod.main()
    before = {p.name: p.read_text() for p in tmp_path.iterdir()}
    assert setup_mod.main() == 0
    assert {p.name: p.read_text() for p in tmp_path.iterdir()} == before


def test_every_install_has_its_own_secret_key(setup_mod, tmp_path, monkeypatch):
    """SECRET_KEY encrypts model keys stored in the portal: a compose install
    always has one, an older install gets one added, and it is then kept."""
    setup_mod.main()
    engine_file = tmp_path / "engine.env"
    first = _env(engine_file.read_text())["SECRET_KEY"]
    assert len(first) >= 32
    # An install made before SECRET_KEY existed: the line is missing.
    engine_file.write_text("".join(line + "\n" for line in engine_file.read_text().splitlines()
                                   if not line.startswith("SECRET_KEY=")))
    assert "SECRET_KEY" not in _env(engine_file.read_text())
    setup_mod.main()
    added = _env(engine_file.read_text())
    assert len(added["SECRET_KEY"]) >= 32 and added["SECRET_KEY"] != first
    assert added["SUPABASE_JWT_SECRET"]  # the rest of the install is untouched
    setup_mod.main()
    assert _env(engine_file.read_text())["SECRET_KEY"] == added["SECRET_KEY"]


def test_local_env_has_a_secret_key(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("gen_env_under_test", ROOT / "deploy/local/gen_env.py")
    gen_env = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen_env)
    env = gen_env.build_env(supabase_url="http://127.0.0.1:54421",
                            database_url_host="127.0.0.1:54432", admin_email="a@engine.local")
    assert len(env["SECRET_KEY"]) >= 32
    env_file = tmp_path / ".env"
    monkeypatch.setattr(gen_env, "ENV_FILE", env_file)
    env_file.write_text("ADMIN_EMAIL=a@engine.local\n")
    assert gen_env.main([]) == 0
    assert "SECRET_KEY=" in env_file.read_text()
    kept = env_file.read_text()
    assert gen_env.main([]) == 0 and env_file.read_text() == kept


def test_two_installs_do_not_share_secrets(setup_mod, tmp_path, monkeypatch):
    setup_mod.main()
    first = _env((tmp_path / "stack.env").read_text())
    other = tmp_path / "other"
    monkeypatch.setenv("ENGINE_STATE_DIR", str(other))
    setup_mod.main()
    second = _env((other / "stack.env").read_text())
    assert first["JWT_SECRET"] != second["JWT_SECRET"]
    assert first["POSTGRES_PASSWORD"] != second["POSTGRES_PASSWORD"]


def test_a_chosen_admin_is_used_and_its_password_is_not_printed(setup_mod, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ADMIN_EMAIL", "owner@corp.test")
    monkeypatch.setenv("ADMIN_PASSWORD", "it's a $ecret; rm -rf")
    setup_mod.main()
    engine_file = tmp_path / "engine.env"
    assert _env(engine_file.read_text())["ADMIN_EMAIL"] == "owner@corp.test"
    assert "$ecret" not in capsys.readouterr().out
    # The file is sourced by a shell: a password with shell characters must
    # come back unchanged and run nothing.
    out = subprocess.run(["sh", "-c", f'. "{engine_file}"; printf %s "$ADMIN_PASSWORD"'],
                         capture_output=True, text=True, check=True).stdout
    assert out == "it's a $ecret; rm -rf"


def test_only_one_of_the_two_admin_settings_stops_setup(setup_mod, tmp_path, monkeypatch, capsys):
    """Half a login is a mistake: nothing is written and the message says so."""
    for name, value in (("ADMIN_EMAIL", "owner@corp.test"), ("ADMIN_PASSWORD", "a long enough passphrase")):
        monkeypatch.delenv("ADMIN_EMAIL", raising=False)
        monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
        monkeypatch.setenv(name, value)
        assert setup_mod.main() == 1
        assert "set both ADMIN_EMAIL and ADMIN_PASSWORD, or neither" in capsys.readouterr().out
        assert list(tmp_path.iterdir()) == []


def _init_with_stubs(monkeypatch, *, admin_created: bool):
    """deploy/compose/init.py with the stack and the schema steps stubbed out.
    Returns (init module, the calls the schema step made)."""
    import sys

    init = _load("init")
    spec = importlib.util.spec_from_file_location("init_db", ROOT / "deploy/local/init_db.py")
    init_db = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(init_db)
    monkeypatch.setitem(sys.modules, "init_db", init_db)
    calls = []

    class _Init:
        def __init__(self, config):
            self.config = config

        def __getattr__(self, name):
            def step(*_a, **_k):
                calls.append(name)
                return MagicMock()
            return step

    monkeypatch.setattr(init_db.common, "DBInitializer", _Init)
    monkeypatch.setattr(init, "wait_for_gateway", lambda *a, **k: None)
    monkeypatch.setattr(init, "admin_exists", lambda dsn, email: calls.append("admin_exists") or admin_created)
    for name in ("AI_DATABASE_URL", "SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_KEY"):
        monkeypatch.setenv(name, "x")
    return init, calls


def test_init_without_an_admin_login_creates_no_account_and_succeeds(monkeypatch, capsys):
    """No ADMIN_*: the stack comes up with no admin; init must not fail for it."""
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    init, calls = _init_with_stubs(monkeypatch, admin_created=False)
    assert init.main() == 0
    assert "apply_migrations" in calls
    assert "ensure_admin_user" not in calls and "admin_exists" not in calls
    out = capsys.readouterr().out
    assert "first-run page" in out and "docker compose logs engine" in out and "[init] ready" in out


def test_init_with_an_admin_login_creates_it_and_fails_when_it_was_not_created(monkeypatch, capsys):
    """The headless path is unchanged: the admin is made at init, or init fails."""
    monkeypatch.setenv("ADMIN_EMAIL", "owner@corp.test")
    monkeypatch.setenv("ADMIN_PASSWORD", "a long enough passphrase")
    init, calls = _init_with_stubs(monkeypatch, admin_created=True)
    assert init.main() == 0
    assert "ensure_admin_user" in calls and "admin_exists" in calls
    assert "first-run page" not in capsys.readouterr().out

    init, calls = _init_with_stubs(monkeypatch, admin_created=False)
    assert init.main() == 1
    assert "the admin account was not created" in capsys.readouterr().out


def test_init_refuses_half_an_admin_login(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAIL", "owner@corp.test")
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    init, calls = _init_with_stubs(monkeypatch, admin_created=False)
    with pytest.raises(SystemExit) as exc:
        init.main()
    assert "set both ADMIN_EMAIL and ADMIN_PASSWORD, or neither" in str(exc.value)
    assert "ensure_admin_user" not in calls


def test_the_hand_run_env_keeps_an_admin_login_unless_told_not_to(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("gen_env_under_test2", ROOT / "deploy/local/gen_env.py")
    gen_env = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen_env)
    env_file = tmp_path / ".env"
    monkeypatch.setattr(gen_env, "ENV_FILE", env_file)
    assert gen_env.main([]) == 0
    env = _env(env_file.read_text())
    assert env["ADMIN_EMAIL"] == "admin@engine.local" and env["ADMIN_PASSWORD"] in capsys.readouterr().out
    assert gen_env.main(["--force", "--no-admin"]) == 0
    env = _env(env_file.read_text())
    assert "ADMIN_EMAIL" not in env and "ADMIN_PASSWORD" not in env
    assert "first-run page" in capsys.readouterr().out


def test_token_helper_points_a_first_run_install_at_api_keys(monkeypatch, capsys):
    token = _load("admin_token")
    monkeypatch.setenv("SUPABASE_URL", "http://gateway:8000")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "anon")
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    assert token.main() == 1
    assert "API key" in capsys.readouterr().err


def test_with_env_loads_settings_and_drops_empty_model_keys(setup_mod, tmp_path):
    setup_mod.main()
    script = ROOT / "deploy" / "compose" / "with_env.sh"
    env = {"PATH": os.environ["PATH"], "ENGINE_STATE_DIR": str(tmp_path),
           "GOOGLE_API_KEY": "", "OPENAI_API_KEY": "sk-kept"}
    out = subprocess.run(
        ["sh", str(script), "sh", "-c",
         'echo "g=${GOOGLE_API_KEY-unset} o=$OPENAI_API_KEY url=$SUPABASE_URL"'],
        capture_output=True, text=True, check=True, env=env).stdout.strip()
    assert out == "g=unset o=sk-kept url=http://gateway:8000"


@pytest.mark.parametrize("given, expected", [(None, "1"), ("", "1"), ("0", "0"), ("1", "1")])
def test_with_env_lets_the_container_override_local_endpoints(setup_mod, tmp_path, given, expected):
    """The setup step stores MCP_ALLOW_LOCAL_ENDPOINTS=1; a value passed to the
    container (compose passes an empty string when the host has none) wins."""
    setup_mod.main()
    script = ROOT / "deploy" / "compose" / "with_env.sh"
    env = {"PATH": os.environ["PATH"], "ENGINE_STATE_DIR": str(tmp_path)}
    if given is not None:
        env["MCP_ALLOW_LOCAL_ENDPOINTS"] = given
    out = subprocess.run(
        ["sh", str(script), "sh", "-c", 'echo "$MCP_ALLOW_LOCAL_ENDPOINTS"'],
        capture_output=True, text=True, check=True, env=env).stdout.strip()
    assert out == expected


def test_with_env_refuses_to_start_before_setup(tmp_path):
    script = ROOT / "deploy" / "compose" / "with_env.sh"
    run = subprocess.run(["sh", str(script), "true"], capture_output=True, text=True,
                         env={"PATH": os.environ["PATH"], "ENGINE_STATE_DIR": str(tmp_path)})
    assert run.returncode == 1 and "setup step has not run" in run.stderr


def test_compose_publishes_only_loopback_ports_and_never_the_database():
    services = _compose()["services"]
    assert "ports" not in services["db"] and "ports" not in services["rest"]
    assert "ports" not in services["auth"]
    for name in ("engine", "gateway"):
        for port in services[name]["ports"]:
            assert port.startswith("127.0.0.1:"), f"{name} publishes {port}"


def test_compose_holds_no_secret_and_orders_the_start():
    compose = _compose()
    services = compose["services"]
    text = yaml.safe_dump(compose)
    for forbidden in ("POSTGRES_PASSWORD", "JWT_SECRET:", "SERVICE_ROLE_KEY:", "SUPABASE_SERVICE_KEY:"):
        assert forbidden not in text, f"{forbidden} belongs in the state volume, not the compose file"
    assert services["db"]["depends_on"]["setup"]["condition"] == "service_completed_successfully"
    assert services["engine"]["depends_on"]["init"]["condition"] == "service_completed_successfully"
    # Every service that needs the secrets mounts the state volume read-only;
    # only setup writes it.
    for name, svc in services.items():
        mounts = [v for v in svc.get("volumes", []) if v.startswith("state:")]
        assert mounts, f"{name} does not mount the state volume"
        assert mounts[0].endswith(":ro") == (name != "setup")


def test_init_fails_when_the_stack_never_answers(monkeypatch):
    init = _load("init")
    monkeypatch.setattr(init.time, "sleep", lambda _s: None)
    with pytest.raises(SystemExit) as exc:
        init.wait_for_gateway("http://127.0.0.1:9", "anon", timeout=0)
    assert "did not come up" in str(exc.value)


def test_token_helper_says_what_is_missing(monkeypatch, capsys):
    token = _load("admin_token")
    for name in ("SUPABASE_URL", "SUPABASE_ANON_KEY", "ADMIN_EMAIL", "ADMIN_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    assert token.main() == 1
    assert "with_env.sh" in capsys.readouterr().err


def test_no_compose_script_shadows_a_standard_library_module():
    """These scripts run with their own folder first on sys.path, so one named
    like a stdlib module (token.py) breaks every script beside it at import."""
    import sys

    names = {p.stem for p in (ROOT / "deploy" / "compose").glob("*.py")}
    assert not names & set(sys.stdlib_module_names)


def test_the_engine_container_knows_the_port_it_listens_on():
    """Scripts run inside the container (the voice text call) find the engine
    through PORT. The image's default is 8080; this stack serves 8000."""
    engine = _compose()["services"]["engine"]
    assert engine["environment"]["PORT"] == "8000"
    assert any(p.endswith(":8000") for p in engine["ports"])
    assert '--port "$$PORT"' in " ".join(engine["command"]) or "--port \"$PORT\"" in " ".join(engine["command"])
