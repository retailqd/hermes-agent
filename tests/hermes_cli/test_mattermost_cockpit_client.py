from __future__ import annotations

import io
import json
import subprocess
import urllib.error
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_cli.mattermost_cockpit.client import (
    HelperBridge,
    HelperBridgeError,
    MattermostClient,
    MattermostClientError,
)


class FakeResponse:
    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None):
        self.status = status
        self._body = body
        self._offset = 0
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self._body) - self._offset
        start = self._offset
        end = min(len(self._body), start + size)
        self._offset = end
        return self._body[start:end]

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name.lower(), default)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def make_client(urlopen):
    return MattermostClient(
        base_url="https://mattermost.example.com",
        token="explicit-token",
        timeout=12.5,
        urlopen=urlopen,
    )


def make_bridge(run: Mock | None = None):
    return HelperBridge(
        owner_post_script=Path("/fake/post_as_owner.py"),
        poll_script=Path("/fake/poll_main.py"),
        watch_script=Path("/fake/watch_main.py"),
        run=run,
    )


class TestMattermostClient:
    def test_requires_strict_base_url_and_token(self):
        with pytest.raises(ValueError):
            MattermostClient(base_url="mattermost.example.com", token="tok")
        with pytest.raises(ValueError):
            MattermostClient(base_url="https://mattermost.example.com", token="")

    def test_get_post_uses_bearer_header_and_explicit_timeout(self):
        captured: dict[str, object] = {}

        def urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["authorization"] = request.headers["Authorization"]
            captured["timeout"] = timeout
            payload = json.dumps({"id": "post-1", "message": "ok"}).encode()
            return FakeResponse(200, payload, {"Content-Type": "application/json"})

        client = make_client(urlopen)
        payload = client.get_post("post-1")

        assert payload == {"id": "post-1", "message": "ok"}
        assert captured["url"] == "https://mattermost.example.com/api/v4/posts/post-1"
        assert captured["authorization"] == "Bearer explicit-token"
        assert captured["timeout"] == 12.5

    def test_get_thread_requires_json_and_redacts_secrets_from_error(self):
        body = (
            b'Authorization: Bearer supersecret-token\n'
            + b"x" * 1500
        )

        def urlopen(request, timeout):
            raise urllib.error.HTTPError(
                request.full_url,
                403,
                "Forbidden",
                hdrs=Message(),
                fp=io.BytesIO(body),
            )

        client = make_client(urlopen)

        with pytest.raises(MattermostClientError) as exc:
            client.get_thread("root-1")

        text = str(exc.value)
        assert "Forbidden" in text
        assert "supersecret-token" not in text
        assert "Bearer explicit-token" not in text
        assert len(text) < 1200

    def test_invalid_json_response_raises_client_error(self):
        def urlopen(request, timeout):
            return FakeResponse(
                200,
                b"not-json",
                {"Content-Type": "application/json"},
            )

        client = make_client(urlopen)

        with pytest.raises(MattermostClientError):
            client.get_channel("channel-1")

    def test_create_post_uses_explicit_token_only(self):
        captured: dict[str, object] = {}

        def urlopen(request, timeout):
            captured["authorization"] = request.headers["Authorization"]
            captured["body"] = json.loads(request.data.decode())
            return FakeResponse(
                201,
                json.dumps({"id": "post-77"}).encode(),
                {"Content-Type": "application/json"},
            )

        client = make_client(urlopen)
        payload = client.create_post("channel-7", "hello", root_id="root-9")

        assert payload == {"id": "post-77"}
        assert captured["authorization"] == "Bearer explicit-token"
        assert captured["body"] == {
            "channel_id": "channel-7",
            "message": "hello",
            "root_id": "root-9",
        }


class TestHelperBridge:
    def test_post_owner_runs_canonical_script_with_stdin_and_reports_post_id(self):
        run = Mock(
            return_value=subprocess.CompletedProcess(
                args=["python3"],
                returncode=0,
                stdout="POSTADO ok | id=post-123 | canal=pht/execucoes | como=retailqd\n",
                stderr="",
            )
        )
        bridge = make_bridge(run)

        result = bridge.post_owner(
            "linha 1\nlinha 2",
            root_id="root-9",
            timeout=17,
        )

        run.assert_called_once()
        called_args, called_kwargs = run.call_args
        assert called_args[0] == [
            "python3",
            "-P",
            "/fake/post_as_owner.py",
            "--team",
            "pht",
            "--channel",
            "execucoes",
            "--root",
            "root-9",
        ]
        assert called_kwargs["input"] == "linha 1\nlinha 2"
        assert called_kwargs["text"] is True
        assert called_kwargs["capture_output"] is True
        assert called_kwargs["timeout"] == 17
        assert result.post_id == "post-123"
        assert result.returncode == 0

    def test_poll_main_redacts_tokens_from_failure_output(self):
        run = Mock(
            return_value=subprocess.CompletedProcess(
                args=["python3"],
                returncode=2,
                stdout="Authorization: Bearer supersecret\nMATTERMOST_TOKEN=topsecret\n",
                stderr="failed with token topsecret\n",
            )
        )
        bridge = make_bridge(run)

        with pytest.raises(HelperBridgeError) as exc:
            bridge.poll_main(thread_id="root-1", timeout=9)

        text = str(exc.value)
        assert "returncode=2" in text
        assert "supersecret" not in text
        assert "topsecret" not in text
        assert "Bearer" not in text or "<redacted>" in text

    def test_watch_main_defaults_to_no_timeout_and_uses_root_arguments(self):
        run = Mock(
            return_value=subprocess.CompletedProcess(
                args=["python3"],
                returncode=0,
                stdout="WAKE: novo relato substantivo em thread root-a\n",
                stderr="",
            )
        )
        bridge = make_bridge(run)

        result = bridge.watch_main(["root-a", "root-b"])

        run.assert_called_once()
        called_args, called_kwargs = run.call_args
        assert called_args[0] == [
            "python3",
            "-P",
            "/fake/watch_main.py",
            "root-a",
            "root-b",
        ]
        assert "timeout" not in called_kwargs
        assert result.returncode == 0
        assert result.stdout.startswith("WAKE:")
