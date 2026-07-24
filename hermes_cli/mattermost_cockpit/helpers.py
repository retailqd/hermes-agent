from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


_TOKEN_RE = re.compile(r"(?i)(MATTERMOST_[A-Z0-9_]*TOKEN\s*=\s*)([^\s\r\n]+)")
_AUTH_RE = re.compile(r"(?i)(Authorization\s*:\s*(?:Bearer|token)\s+)([^\s\r\n]+)")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+([^\s\r\n]+)")
_ID_RE = re.compile(r"\bid=([^\s|]+)")


@dataclass(slots=True)
class HelperRunResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    post_id: str | None = None


class HelperBridgeError(RuntimeError):
    pass


class HelperBridge:
    def __init__(
        self,
        *,
        owner_post_script: Path | str,
        poll_script: Path | str,
        watch_script: Path | str,
        python_bin: str = "python3",
        default_team: str = "pht",
        default_channel: str = "execucoes",
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.owner_post_script = Path(owner_post_script)
        self.poll_script = Path(poll_script)
        self.watch_script = Path(watch_script)
        self.python_bin = python_bin
        self.default_team = default_team
        self.default_channel = default_channel
        self._run = run

    def post_owner(
        self,
        message: str,
        *,
        timeout: float,
        team: str | None = None,
        channel: str | None = None,
        root_id: str | None = None,
    ) -> HelperRunResult:
        command = [
            self.python_bin,
            "-P",
            str(self.owner_post_script),
            "--team",
            team or self.default_team,
            "--channel",
            channel or self.default_channel,
        ]
        if root_id:
            command.extend(["--root", root_id])
        return self._run_script(command, input_text=message, timeout=timeout, label="owner-post")

    def poll_main(
        self,
        *,
        timeout: float,
        thread_id: str | None = None,
        channel: str | None = None,
        state: str | None = None,
        after_post: str | None = None,
        init: bool = False,
        include_logs: bool = False,
        max_pages: int | None = None,
    ) -> HelperRunResult:
        command = [self.python_bin, "-P", str(self.poll_script)]
        if channel or self.default_channel:
            command.extend(["--channel", channel or self.default_channel])
        if thread_id:
            command.extend(["--thread", thread_id])
        if state:
            command.extend(["--state", state])
        if after_post:
            command.extend(["--after-post", after_post])
        if init:
            command.append("--init")
        if include_logs:
            command.append("--include-logs")
        if max_pages is not None:
            command.extend(["--max-pages", str(max_pages)])
        return self._run_script(command, timeout=timeout, label="poll-main")

    def watch_main(
        self,
        root_ids: Sequence[str],
        *,
        timeout: float | None = None,
    ) -> HelperRunResult:
        command = [self.python_bin, "-P", str(self.watch_script), *[str(root) for root in root_ids]]
        return self._run_script(command, timeout=timeout, label="watch-main")

    def _run_script(
        self,
        command: list[str],
        *,
        timeout: float | None,
        label: str,
        input_text: str | None = None,
    ) -> HelperRunResult:
        kwargs: dict[str, object] = {"capture_output": True, "text": True}
        if timeout is not None:
            kwargs["timeout"] = timeout
        if input_text is not None:
            kwargs["input"] = input_text

        try:
            completed = self._run(command, **kwargs)
        except subprocess.TimeoutExpired as exc:  # pragma: no cover - exercised via tests if injected
            raise HelperBridgeError(
                f"{label} timed out after {exc.timeout!r}s: {self._format_command(command)}"
            ) from exc
        except OSError as exc:
            raise HelperBridgeError(
                f"{label} failed to start: {self._format_command(command)} ({exc})"
            ) from exc

        stdout = self._redact(completed.stdout or "")
        stderr = self._redact(completed.stderr or "")
        result = HelperRunResult(
            command=tuple(command),
            returncode=int(completed.returncode),
            stdout=stdout,
            stderr=stderr,
            post_id=self._extract_post_id(stdout) if label == "owner-post" else None,
        )
        if result.returncode != 0:
            raise HelperBridgeError(self._format_failure(label, result))
        if label == "owner-post" and not result.post_id:
            raise HelperBridgeError(
                f"owner-post succeeded but did not return a post id: {self._format_command(command)}"
            )
        return result

    def _format_failure(self, label: str, result: HelperRunResult) -> str:
        stdout = result.stdout.strip() or "<empty>"
        stderr = result.stderr.strip() or "<empty>"
        return (
            f"{label} failed (returncode={result.returncode}): "
            f"{self._format_command(result.command)}\n"
            f"stdout: {stdout}\n"
            f"stderr: {stderr}"
        )

    @staticmethod
    def _format_command(command: Sequence[str]) -> str:
        return " ".join(command)

    @staticmethod
    def _extract_post_id(stdout: str) -> str | None:
        match = _ID_RE.search(stdout)
        return match.group(1) if match else None

    @staticmethod
    def _redact(text: str) -> str:
        text = _TOKEN_RE.sub(r"\1<redacted>", text)
        text = _AUTH_RE.sub(r"\1<redacted>", text)
        text = _BEARER_RE.sub("Bearer <redacted>", text)
        return text
