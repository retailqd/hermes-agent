from __future__ import annotations

import io
import json
import subprocess
import sys
import urllib.error
import urllib.parse
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

    def test_get_reactions_accepts_array_response(self):
        captured: dict[str, object] = {}

        def urlopen(request, timeout):
            captured["url"] = request.full_url
            return FakeResponse(
                200,
                json.dumps(
                    [{"user_id": "bot-1", "post_id": "post-1", "emoji_name": "eyes"}]
                ).encode(),
                {"Content-Type": "application/json"},
            )

        reactions = make_client(urlopen).get_reactions("post-1")

        assert reactions == [
            {"user_id": "bot-1", "post_id": "post-1", "emoji_name": "eyes"}
        ]
        assert captured["url"] == (
            "https://mattermost.example.com/api/v4/posts/post-1/reactions"
        )

    def test_get_reactions_rejects_non_array_response(self):
        def urlopen(request, timeout):
            return FakeResponse(
                200,
                json.dumps({"emoji_name": "eyes"}).encode(),
                {"Content-Type": "application/json"},
            )

        with pytest.raises(MattermostClientError, match="invalid JSON"):
            make_client(urlopen).get_reactions("post-1")

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

    def test_search_posts_uses_exact_team_endpoint_and_and_search(self):
        captured: dict[str, object] = {}

        def urlopen(request, timeout):
            captured["method"] = request.method
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode())
            return FakeResponse(
                200,
                json.dumps({"order": [], "posts": {}}).encode(),
                {"Content-Type": "application/json"},
            )

        client = make_client(urlopen)
        payload = client.search_posts("team-1", "[cockpit-task:task-one]")

        assert payload == {"order": [], "posts": {}}
        assert captured["method"] == "POST"
        assert str(captured["url"]).endswith("/api/v4/teams/team-1/posts/search")
        assert captured["body"] == {
            "terms": "[cockpit-task:task-one]",
            "is_or_search": False,
        }

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
        payload = client.create_post(
            "channel-7",
            "hello",
            root_id="root-9",
            props={"cockpit_relay_marker": "relay-1", "cockpit_relay_schema": 1},
        )

        assert payload == {"id": "post-77"}
        assert captured["authorization"] == "Bearer explicit-token"
        assert captured["body"] == {
            "channel_id": "channel-7",
            "message": "hello",
            "root_id": "root-9",
            "props": {"cockpit_relay_marker": "relay-1", "cockpit_relay_schema": 1},
        }

    def test_delete_post_uses_exact_endpoint_and_no_body(self):
        captured: dict[str, object] = {}

        def urlopen(request, timeout):
            captured["method"] = request.method
            captured["url"] = request.full_url
            captured["body"] = request.data
            return FakeResponse(
                200,
                json.dumps({"status": "OK"}).encode(),
                {"Content-Type": "application/json"},
            )

        client = make_client(urlopen)

        assert client.delete_post("post-77") == {"status": "OK"}
        assert captured == {
            "method": "DELETE",
            "url": "https://mattermost.example.com/api/v4/posts/post-77",
            "body": None,
        }

    def test_update_post_includes_required_post_id(self):
        captured: dict[str, object] = {}

        def urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode())
            return FakeResponse(
                200,
                json.dumps({"id": "post-77", "message": "updated"}).encode(),
                {"Content-Type": "application/json"},
            )

        client = make_client(urlopen)
        client.update_post("post-77", "updated")

        assert str(captured["url"]).endswith("/api/v4/posts/post-77")
        assert captured["body"] == {"id": "post-77", "message": "updated"}

    def test_update_post_can_preserve_props_on_edit(self):
        captured: dict[str, object] = {}

        def urlopen(request, timeout):
            captured["body"] = json.loads(request.data.decode())
            return FakeResponse(
                200,
                json.dumps({"id": "post-77", "message": "updated"}).encode(),
                {"Content-Type": "application/json"},
            )

        client = make_client(urlopen)
        client.update_post(
            "post-77",
            "updated",
            props={"cockpit_relay_marker": "[cockpit-status:task-77]", "cockpit_relay_schema": 1},
        )

        assert captured["body"] == {
            "id": "post-77",
            "message": "updated",
            "props": {
                "cockpit_relay_marker": "[cockpit-status:task-77]",
                "cockpit_relay_schema": 1,
            },
        }

    @pytest.mark.parametrize("following,expected_method", [(True, "PUT"), (False, "DELETE")])
    def test_set_thread_following_uses_user_team_thread_endpoint(self, following, expected_method):
        captured: dict[str, object] = {}

        def urlopen(request, timeout):
            captured["method"] = request.method
            captured["url"] = request.full_url
            captured["body"] = None if request.data is None else json.loads(request.data.decode())
            return FakeResponse(
                200,
                json.dumps({"status": "OK"}).encode(),
                {"Content-Type": "application/json"},
            )

        client = make_client(urlopen)
        client.set_thread_following(
            user_id="user-1",
            team_id="team-1",
            thread_id="root-1",
            following=following,
        )

        assert captured["method"] == expected_method
        assert str(captured["url"]).endswith(
            "/api/v4/users/me/teams/team-1/threads/root-1/following"
        )
        assert captured["body"] is None

    def test_is_thread_following_reads_cursor_paginated_thread_list(self):
        urls: list[str] = []

        def urlopen(request, timeout):
            urls.append(request.full_url)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
            before = query.get("before", [None])[0]
            threads = (
                [{"id": f"other-{index}"} for index in range(200)]
                if before is None
                else [{"id": "root-1", "post": {"id": "root-1"}}]
            )
            return FakeResponse(
                200,
                json.dumps({"threads": threads}).encode(),
                {"Content-Type": "application/json"},
            )

        client = make_client(urlopen)
        assert client.is_thread_following(
            user_id="user-1", team_id="team-1", thread_id="root-1"
        ) is True
        assert len(urls) == 2
        assert urls[0].endswith(
            "/api/v4/users/me/teams/team-1/threads?per_page=200&extended=false"
        )
        assert urls[1].endswith("&before=other-199")

    def test_is_thread_following_returns_false_after_last_cursor_page(self):
        def urlopen(request, timeout):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)
            before = query.get("before", [None])[0]
            threads = (
                [{"id": f"other-{index}"} for index in range(200)]
                if before is None
                else []
            )
            return FakeResponse(
                200,
                json.dumps({"threads": threads}).encode(),
                {"Content-Type": "application/json"},
            )

        client = make_client(urlopen)
        assert client.is_thread_following(
            user_id="user-1", team_id="team-1", thread_id="root-1"
        ) is False

    def test_is_thread_following_rejects_repeated_cursor(self):
        def urlopen(request, timeout):
            threads = [{"id": f"other-{index}"} for index in range(200)]
            return FakeResponse(
                200,
                json.dumps({"threads": threads}).encode(),
                {"Content-Type": "application/json"},
            )

        client = make_client(urlopen)
        with pytest.raises(MattermostClientError, match="repeated or invalid cursor"):
            client.is_thread_following(
                user_id="user-1", team_id="team-1", thread_id="root-1"
            )

    def test_is_thread_following_rejects_invalid_list_response(self):
        def urlopen(request, timeout):
            return FakeResponse(
                200,
                json.dumps({"threads": {}}).encode(),
                {"Content-Type": "application/json"},
            )

        client = make_client(urlopen)
        with pytest.raises(MattermostClientError, match="invalid threads"):
            client.is_thread_following(
                user_id="user-1", team_id="team-1", thread_id="root-1"
            )

    def test_success_response_body_is_bounded(self):
        def urlopen(request, timeout):
            return FakeResponse(
                200,
                b'{' + b'"payload":"' + b"x" * (16 * 1024 * 1024) + b'"}',
                {"Content-Type": "application/json"},
            )

        client = make_client(urlopen)
        with pytest.raises(MattermostClientError, match="too large"):
            client.get_thread("root-1")

    def test_url_error_reason_is_redacted(self):
        def urlopen(request, timeout):
            raise urllib.error.URLError("Authorization: Bearer network-secret-token")

        client = make_client(urlopen)
        with pytest.raises(MattermostClientError) as exc:
            client.get_post("post-1")

        assert "network-secret-token" not in str(exc.value)


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
            sys.executable,
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
            sys.executable,
            "-P",
            "/fake/watch_main.py",
            "root-a",
            "root-b",
        ]
        assert "timeout" not in called_kwargs
        assert result.returncode == 0
        assert result.stdout.startswith("WAKE:")

    def test_watch_main_rejects_empty_root_list(self):
        bridge = make_bridge(Mock())

        with pytest.raises(ValueError, match="root"):
            bridge.watch_main([])
