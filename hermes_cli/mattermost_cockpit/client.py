from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

from hermes_cli.mattermost_cockpit.helpers import (
    HelperBridge,
    HelperBridgeError,
    HelperRunResult,
)


_JSON_LIMIT = 4096
_MAX_JSON_BODY = 16 * 1024 * 1024
_ERROR_BODY_LIMIT = 1024
_THREADS_PER_PAGE = 200
_MAX_THREAD_PAGES = 100
_AUTH_RE = re.compile(r"(?i)(Authorization\s*:\s*(?:Bearer|token)\s+)([^\s\r\n]+)")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+([^\s\r\n]+)")
_GENERIC_TOKEN_RE = re.compile(r"(?i)\btoken(?:\s*[:=]?\s*)([^\s\r\n]+)")


class MattermostClientError(RuntimeError):
    pass


class MattermostAPIError(MattermostClientError):
    def __init__(self, status_code: int, method: str, url: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.method = method
        self.url = url


class MattermostClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout: float = 15.0,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
        request_factory: Callable[..., urllib.request.Request] = urllib.request.Request,
    ) -> None:
        self.base_url = self._normalize_base_url(base_url)
        self.token = self._normalize_token(token)
        self.timeout = float(timeout)
        self._urlopen = urlopen
        self._request_factory = request_factory

    def get_post(self, post_id: str) -> dict[str, Any]:
        return self._request_json_object("GET", f"/api/v4/posts/{post_id}")

    def get_thread(self, post_id: str) -> dict[str, Any]:
        return self._request_json_object("GET", f"/api/v4/posts/{post_id}/thread")

    def get_channel(self, channel_id: str) -> dict[str, Any]:
        return self._request_json_object("GET", f"/api/v4/channels/{channel_id}")

    def get_channel_by_name(self, team_name: str, channel_name: str) -> dict[str, Any]:
        return self._request_json_object(
            "GET",
            f"/api/v4/teams/name/{team_name}/channels/name/{channel_name}",
        )

    def get_user(self, user_id: str) -> dict[str, Any]:
        return self._request_json_object("GET", f"/api/v4/users/{user_id}")

    def get_user_by_username(self, username: str) -> dict[str, Any]:
        return self._request_json_object("GET", f"/api/v4/users/username/{username}")

    def search_posts(self, team_id: str, terms: str) -> dict[str, Any]:
        return self._request_json_object(
            "POST",
            f"/api/v4/teams/{team_id}/posts/search",
            {"terms": terms, "is_or_search": False},
        )

    def create_post(
        self,
        channel_id: str,
        message: str,
        *,
        root_id: str | None = None,
        props: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"channel_id": channel_id, "message": message}
        if root_id:
            payload["root_id"] = root_id
        if props is not None:
            payload["props"] = dict(props)
        return self._request_json_object("POST", "/api/v4/posts", payload)

    def delete_post(self, post_id: str) -> dict[str, Any]:
        return self._request_json_object("DELETE", f"/api/v4/posts/{post_id}")

    def update_post(
        self,
        post_id: str,
        message: str,
        *,
        props: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": post_id, "message": message}
        if props is not None:
            payload["props"] = dict(props)
        return self._request_json_object("PUT", f"/api/v4/posts/{post_id}", payload)

    def add_reaction(self, *, user_id: str, post_id: str, emoji_name: str) -> dict[str, Any]:
        return self._request_json_object(
            "POST",
            "/api/v4/reactions",
            {"user_id": user_id, "post_id": post_id, "emoji_name": emoji_name},
        )

    def remove_reaction(self, *, user_id: str, post_id: str, emoji_name: str) -> dict[str, Any]:
        return self._request_json_object(
            "DELETE",
            f"/api/v4/users/{user_id}/posts/{post_id}/reactions/{emoji_name}",
        )

    def get_reactions(self, post_id: str) -> list[dict[str, Any]]:
        response = self._perform_request("GET", f"/api/v4/posts/{post_id}/reactions")
        if not isinstance(response, list) or any(
            not isinstance(item, dict) for item in response
        ):
            raise MattermostClientError(
                f"GET /api/v4/posts/{post_id}/reactions returned invalid JSON"
            )
        return response

    def set_thread_following(
        self,
        *,
        user_id: str,
        team_id: str,
        thread_id: str,
        following: bool,
    ) -> dict[str, Any]:
        method = "PUT" if following else "DELETE"
        return self._request_json_object(
            method,
            f"/api/v4/users/me/teams/{team_id}/threads/{thread_id}/following",
        )

    def is_thread_following(
        self, *, user_id: str, team_id: str, thread_id: str
    ) -> bool:
        _ = user_id  # The authenticated owner's `me` route is the readback authority.
        before: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(_MAX_THREAD_PAGES):
            path = (
                f"/api/v4/users/me/teams/{team_id}/threads"
                f"?per_page={_THREADS_PER_PAGE}&extended=false"
            )
            if before is not None:
                path += f"&before={urllib.parse.quote(before, safe='')}"
            payload = self._request_json_object("GET", path)
            threads = payload.get("threads")
            if not isinstance(threads, list):
                raise MattermostClientError(
                    "thread following readback returned invalid threads"
                )
            for thread in threads:
                if not isinstance(thread, dict):
                    continue
                post = thread.get("post")
                if thread.get("id") == thread_id or (
                    isinstance(post, dict) and post.get("id") == thread_id
                ):
                    return True
            if len(threads) < _THREADS_PER_PAGE:
                return False
            last_thread = threads[-1]
            if not isinstance(last_thread, dict):
                raise MattermostClientError(
                    "thread following readback returned invalid cursor"
                )
            cursor = last_thread.get("id")
            if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                raise MattermostClientError(
                    "thread following readback repeated or invalid cursor"
                )
            seen_cursors.add(cursor)
            before = cursor
        raise MattermostClientError(
            "thread following readback exceeded pagination limit"
        )

    def _request_json_object(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        response = self._perform_request(method, path, data=data)
        if not isinstance(response, dict):
            raise MattermostClientError(
                f"{method} {path} returned non-object JSON ({type(response).__name__})"
            )
        return response

    def _perform_request(
        self,
        method: str,
        path: str,
        *,
        data: bytes | None = None,
    ) -> Any:
        request = self._build_request(method, path, data=data)
        try:
            with self._urlopen(request, timeout=self.timeout) as response:
                status = int(getattr(response, "status", 200) or 200)
                if not 200 <= status < 300:
                    body = response.read(_ERROR_BODY_LIMIT + 1)
                    raise self._error_from_body(
                        method,
                        request.full_url,
                        status,
                        body,
                        reason=getattr(response, "reason", None),
                    )
                body = response.read(_MAX_JSON_BODY + 1)
                if len(body) > _MAX_JSON_BODY:
                    raise MattermostClientError(
                        f"{method} {request.full_url} returned a JSON body that is too large"
                    )
                return self._decode_json_body(method, request.full_url, body, response.headers)
        except urllib.error.HTTPError as exc:
            body = exc.read(_ERROR_BODY_LIMIT + 1)
            raise self._error_from_body(
                method,
                request.full_url,
                int(exc.code),
                body,
                reason=getattr(exc, "reason", None),
            ) from exc
        except urllib.error.URLError as exc:
            reason = self._redact(str(exc.reason))
            raise MattermostClientError(f"{method} {request.full_url} failed: {reason}") from exc
        except UnicodeDecodeError as exc:
            raise MattermostClientError(f"{method} {request.full_url} returned invalid utf-8") from exc

    def _build_request(self, method: str, path: str, *, data: bytes | None) -> urllib.request.Request:
        url = self._join_url(path)
        request = self._request_factory(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        return request

    def _decode_json_body(
        self,
        method: str,
        url: str,
        body: bytes,
        headers: Any,
    ) -> Any:
        if not body:
            raise MattermostClientError(f"{method} {url} returned an empty body")
        text = body.decode("utf-8")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            preview = self._redact(_truncate(text, _JSON_LIMIT))
            raise MattermostClientError(
                f"{method} {url} returned invalid JSON: {preview}"
            ) from exc
        if not isinstance(parsed, (dict, list)):
            raise MattermostClientError(
                f"{method} {url} returned unsupported JSON type {type(parsed).__name__}"
            )
        return parsed

    def _error_from_body(
        self,
        method: str,
        url: str,
        status_code: int,
        body: bytes,
        *,
        reason: str | None,
    ) -> MattermostAPIError:
        text = body.decode("utf-8", errors="replace")
        if len(body) > _ERROR_BODY_LIMIT:
            text = _truncate(text, _ERROR_BODY_LIMIT) + "…[truncated]"
        text = self._redact(text)
        reason_text = f" {reason}" if reason else ""
        message = f"{method} {url} failed with HTTP {status_code}{reason_text}: {text or '<empty>'}"
        return MattermostAPIError(status_code, method, url, message)

    def _join_url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self.base_url + path

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        parsed = urllib.parse.urlsplit(base_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute http(s) URL")
        normalized_path = parsed.path.rstrip("/")
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, normalized_path, "", ""))

    @staticmethod
    def _normalize_token(token: str) -> str:
        token = token.strip()
        if not token:
            raise ValueError("token is required")
        return token

    def _redact(self, text: str) -> str:
        text = text.replace(f"Bearer {self.token}", "Bearer <redacted>")
        text = text.replace(self.token, "<redacted>")
        text = _AUTH_RE.sub(r"\1<redacted>", text)
        text = _BEARER_RE.sub("Bearer <redacted>", text)
        text = _GENERIC_TOKEN_RE.sub("token <redacted>", text)
        return text


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit]


__all__ = [
    "HelperBridge",
    "HelperBridgeError",
    "HelperRunResult",
    "MattermostAPIError",
    "MattermostClient",
    "MattermostClientError",
]
