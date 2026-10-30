"""The client address behind the reverse proxy, through a real uvicorn started by `jorgex-social
serve`: the sign-in limit counts the address the trusted proxy forwards, and ignores the
X-Forwarded-For of anyone else. 127.0.0.3 plays the proxy (the Docker gateway in compose.yaml);
127.0.0.1, which uvicorn would trust by default, plays a client reaching the port directly, so the
tests also prove the configured address replaces uvicorn's default."""

import http.client
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from urllib.parse import urlencode

import pytest

PUBLIC = "https://social.example.com"
PROXY, OUTSIDER = "127.0.0.3", "127.0.0.1"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _can_bind(address: str) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 9), timeout=0.5, source_address=(address, 0)):
            pass
    except ConnectionRefusedError:
        return True
    except OSError:
        return False
    return True


pytestmark = pytest.mark.skipif(not _can_bind(PROXY), reason=f"{PROXY} is not usable on this host")


@contextmanager
def serving(settings):
    """Yields the port and a list that holds the server's output once it has stopped."""
    port = _free_port()
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("JORGEX_SOCIAL_") and k != "FORWARDED_ALLOW_IPS"
    }
    env |= {
        "JORGEX_SOCIAL_DATABASE_PATH": str(settings.database_path),
        "JORGEX_SOCIAL_MEDIA_DIR": str(settings.media_dir),
        "JORGEX_SOCIAL_SECRET_KEY_FILE": str(settings.secret_key_file),
        "JORGEX_SOCIAL_PUBLIC_URL": PUBLIC,
        "JORGEX_SOCIAL_FORWARDED_ALLOW_IPS": PROXY,
    }
    process = subprocess.Popen(
        [sys.executable, "-c", "from jorgex_social.cli import main; main()", "serve"]
        + ["--host", "127.0.0.1", "--port", str(port)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output = []
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                if _request(port, "GET", "/health")[0] == 200:
                    break
            except OSError:
                pass
            assert process.poll() is None, "the server exited"
            assert time.monotonic() < deadline, "the server did not start"
            time.sleep(0.1)
        yield port, output
    finally:
        process.terminate()
        try:
            out, _ = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            out, _ = process.communicate()
        output.append(out)


@pytest.fixture
def server(settings):
    with serving(settings) as (port, _):
        yield port


def _request(port, method, path, *, source=OUTSIDER, headers=None, body=None) -> tuple[int, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10, source_address=(source, 0))
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read().decode()
    finally:
        conn.close()


def _sign_in(port, source, forwarded_for) -> int:
    body = urlencode({"password": "not the password", "code": "123456"})
    headers = {
        "origin": PUBLIC,
        "content-type": "application/x-www-form-urlencoded",
        "x-forwarded-for": forwarded_for,
    }
    return _request(port, "POST", "/ui/login", source=source, headers=headers, body=body)[0]


def _refused_clients(output: list[str]) -> list[str]:
    lines = [json.loads(line) for line in "".join(output).splitlines() if line.startswith("{")]
    return [line["client"] for line in lines if line["message"].endswith("too many attempts")]


def test_the_trusted_proxy_forwards_the_client_address(server):
    port = server
    for _ in range(5):
        assert _sign_in(port, PROXY, "203.0.113.7") == 401

    assert _sign_in(port, PROXY, "203.0.113.7") == 429
    assert _sign_in(port, PROXY, "203.0.113.8") == 401  # another client, its own count

    forwarded = {"x-forwarded-for": "203.0.113.7"}
    assert _request(port, "GET", "/v1/token", source=PROXY, headers=forwarded)[0] == 404
    assert _request(port, "GET", "/v1/token", source=PROXY)[0] == 401  # the tunnel


def test_a_forged_header_from_anyone_else_does_not_count(server):
    port = server
    for n in range(5):
        assert _sign_in(port, OUTSIDER, f"203.0.113.{n + 20}") == 401

    # Every attempt counted against the peer itself, whatever address it claimed.
    assert _sign_in(port, OUTSIDER, "203.0.113.99") == 429
    assert _sign_in(port, PROXY, "203.0.113.99") == 401


def test_the_log_names_the_address_that_was_counted(settings):
    """The line an operator can look for after setting up the proxy."""
    with serving(settings) as (port, output):
        for _ in range(6):
            _sign_in(port, PROXY, "203.0.113.7")
            _sign_in(port, OUTSIDER, "203.0.113.50")

    assert _refused_clients(output) == ["203.0.113.7", OUTSIDER]
