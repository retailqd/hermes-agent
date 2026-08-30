"""Verified, profile-scoped owner contract for every Hermes session.

The contract is built and activated by ``brainctl`` as an immutable,
content-addressed release.  Hermes only consumes the atomic ``current``
pointer.  Once the ``required`` marker exists, any missing or corrupt member
is a hard startup error rather than a silent fallback to weaker behavior.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home


SCHEMA = 1
TARGET = "hermes"
MAX_CONTRACT_BYTES = 24 * 1024
INSTALL_ROOT = "owner-contract"
CONTRACT_FILE = "OWNER_CONTRACT.md"
MANIFEST_FILE = "manifest.json"
CONTRACT_ID_LABEL = "Owner-Contract-ID"


class OwnerContractError(RuntimeError):
    """The configured owner contract cannot be trusted or loaded."""


@dataclass(frozen=True)
class LoadedOwnerContract:
    content: str
    contract_id: str
    manifest: dict[str, Any]
    release: Path

    def prompt_block(self) -> str:
        return f"{self.content.rstrip()}\n\n{CONTRACT_ID_LABEL}: {self.contract_id}"


def _is_regular_file(path: Path) -> bool:
    try:
        value = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(value.st_mode) and not path.is_symlink()


def _installation_paths() -> tuple[Path, Path, Path]:
    root = get_hermes_home() / INSTALL_ROOT
    return root, root / "current", root / "required"


def load_owner_contract() -> LoadedOwnerContract | None:
    """Load and verify the active contract, or return ``None`` if unconfigured.

    The absence of both the activation pointer and required marker preserves
    upstream Hermes behavior for installations that have never opted in.  As
    soon as either activation artifact exists, validation is fail-closed.
    """
    root, current, required = _installation_paths()
    configured = (
        current.is_symlink()
        or current.exists()
        or required.exists()
        or required.is_symlink()
    )
    if not configured:
        return None
    if not _is_regular_file(required):
        raise OwnerContractError(
            "owner contract is required but its activation marker is missing or unsafe"
        )
    try:
        if required.read_text(encoding="utf-8") != "schema=1\n":
            raise OwnerContractError("owner contract activation marker is invalid")
    except OSError as exc:
        raise OwnerContractError(
            "owner contract activation marker cannot be read"
        ) from exc
    if not current.is_symlink():
        raise OwnerContractError(
            "owner contract is required but the current pointer is missing or unsafe"
        )

    releases = root / "releases"
    try:
        release = current.resolve(strict=True)
        release.relative_to(releases.resolve(strict=True))
        release_stat = release.lstat()
    except (OSError, ValueError) as exc:
        raise OwnerContractError(
            "owner contract current pointer escapes the release root"
        ) from exc
    if not stat.S_ISDIR(release_stat.st_mode) or release.is_symlink():
        raise OwnerContractError("owner contract release directory is unsafe")
    if not re.fullmatch(r"[0-9a-f]{64}", release.name):
        raise OwnerContractError("owner contract release id is invalid")

    contract_path = release / CONTRACT_FILE
    manifest_path = release / MANIFEST_FILE
    if not _is_regular_file(contract_path) or not _is_regular_file(manifest_path):
        raise OwnerContractError("owner contract release members are missing or unsafe")
    try:
        content_bytes = contract_path.read_bytes()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OwnerContractError("owner contract release cannot be decoded") from exc
    if not content_bytes or len(content_bytes) > MAX_CONTRACT_BYTES:
        raise OwnerContractError(
            "owner contract content is empty or exceeds its byte budget"
        )
    required_keys = {
        "schema",
        "target",
        "host",
        "id",
        "sha256",
        "bytes",
        "required_skills",
    }
    if not isinstance(manifest, dict) or set(manifest) != required_keys:
        raise OwnerContractError("owner contract manifest shape is invalid")
    if manifest["schema"] != SCHEMA or manifest["target"] != TARGET:
        raise OwnerContractError("owner contract manifest version or target is invalid")
    contract_id = manifest["id"]
    if not isinstance(contract_id, str) or not re.fullmatch(
        r"[0-9a-f]{64}", contract_id
    ):
        raise OwnerContractError("owner contract manifest id is invalid")
    digest = hashlib.sha256(content_bytes).hexdigest()
    if (
        contract_id != release.name
        or manifest["sha256"] != digest
        or contract_id != digest
        or manifest["bytes"] != len(content_bytes)
    ):
        raise OwnerContractError("owner contract integrity check failed")
    if manifest["host"] not in {"kubuntu", "zenbook"}:
        raise OwnerContractError("owner contract host is invalid")
    skills = manifest["required_skills"]
    if (
        not isinstance(skills, list)
        or any(
            not isinstance(name, str) or not re.fullmatch(r"[a-z0-9-]{1,64}", name)
            for name in skills
        )
        or len(skills) != len(set(skills))
    ):
        raise OwnerContractError("owner contract required-skills manifest is invalid")
    hermes_home = get_hermes_home()
    missing = [
        name
        for name in skills
        if not (hermes_home / "skills" / name / "SKILL.md").is_file()
    ]
    if missing:
        raise OwnerContractError(
            f"owner contract required skill is missing: {', '.join(missing)}"
        )
    try:
        content = content_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OwnerContractError("owner contract content is not valid UTF-8") from exc
    return LoadedOwnerContract(
        content=content,
        contract_id=contract_id,
        manifest=manifest,
        release=release,
    )


def active_owner_contract_id() -> str | None:
    loaded = load_owner_contract()
    return loaded.contract_id if loaded else None


def prompt_matches_active_owner_contract(prompt: str) -> bool:
    """Return whether a stored prompt has the currently active contract id.

    Invalid configured state raises :class:`OwnerContractError`; callers must
    not turn a corrupt required contract into a fail-open cache reuse.
    """
    active_id = active_owner_contract_id()
    if active_id is None:
        return True
    prefix = f"{CONTRACT_ID_LABEL}:"
    stored_id = ""
    for line in prompt.splitlines():
        if line.startswith(prefix):
            stored_id = line[len(prefix) :].strip()
    return stored_id == active_id


def harden_tool_guardrail_config(config: Any) -> dict[str, Any]:
    """Apply the owner's executable no-progress floor when a contract is active."""
    source = dict(config) if isinstance(config, dict) else {}
    if active_owner_contract_id() is None:
        return source

    def bounded_threshold(value: Any, *, fallback: int, ceiling: int) -> int:
        """Return a positive threshold no weaker than the contract ceiling.

        Profile configuration is user-editable and may contain booleans,
        floats, arbitrary strings, or negative values.  A malformed value
        must never make owner-contract activation skip the guardrail
        controller, so normalize locally instead of letting ``int`` escape.
        """
        if isinstance(value, bool):
            return fallback
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            return fallback
        if parsed < 1:
            return fallback
        return min(parsed, ceiling)

    source["warnings_enabled"] = True
    source["hard_stop_enabled"] = True
    warn_after = (
        dict(source.get("warn_after"))
        if isinstance(source.get("warn_after"), dict)
        else {}
    )
    hard_stop_after = (
        dict(source.get("hard_stop_after"))
        if isinstance(source.get("hard_stop_after"), dict)
        else {}
    )
    warn_after.update({
        "exact_failure": bounded_threshold(
            warn_after.get("exact_failure"), fallback=2, ceiling=2
        ),
        "same_tool_failure": bounded_threshold(
            warn_after.get("same_tool_failure"), fallback=3, ceiling=3
        ),
        "idempotent_no_progress": bounded_threshold(
            warn_after.get("idempotent_no_progress"), fallback=2, ceiling=2
        ),
    })
    hard_stop_after.update({
        "exact_failure": bounded_threshold(
            hard_stop_after.get("exact_failure"), fallback=3, ceiling=3
        ),
        "same_tool_failure": bounded_threshold(
            hard_stop_after.get("same_tool_failure"), fallback=5, ceiling=5
        ),
        "idempotent_no_progress": bounded_threshold(
            hard_stop_after.get("idempotent_no_progress"), fallback=2, ceiling=2
        ),
    })
    source["warn_after"] = warn_after
    source["hard_stop_after"] = hard_stop_after
    return source
