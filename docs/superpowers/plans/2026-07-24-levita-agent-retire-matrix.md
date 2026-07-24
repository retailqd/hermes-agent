# Levita Agent Retirement and Private Matrix Intake Implementation Plan

> **For Hermes execution:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development` to execute this plan task by task. Every production mutation remains bound to cockpit task `levita-agent-retire-matrix-20260724`.

**Goal:** Produce a verified, restorable external backup of the Levita sales-agent runtime, selectively retire only proved unused components, then deploy and live-validate a private Matrix intake that routes through Mattermost `main` and the existing `Execuções` cockpit.

**Architecture:** Use a serialized safety pipeline: read-only production triage, immutable backup bundle, independent PASS gate, selective retirement, pinned Synapse/PostgreSQL deployment, restricted Hermes profile/plugin activation, and owner-device validation. Matrix is intake and relay only. Mattermost `main` remains the canonical task and decision surface, and the bound `Execuções` root remains the only executor.

**Tech stack:** Linux, SSH target `phantom`, Docker and Coolify, PostgreSQL tools, Python 3.13 with `uv`, pytest, SQLite, Synapse, Matrix Client-Server API, Hermes Agent profiles/plugins, Mattermost cockpit CLI, systemd user units.

**Authorization:** `APPROVED_BY_OWNER: standing-scope`. This covers the ordinary lifecycle in this plan. Hold only unresolved DNS/domain choice, secret disclosure/rotation outside deployment bootstrap, destructive data actions outside the proved candidate set, and external customer/supplier/fiscal/money/shipping actions.

**Primary design:** `docs/superpowers/specs/2026-07-24-levita-agent-retire-matrix-design.md`

**Execution repository:** `/home/pht2/levita-matrix-intake`

**External backup root:** `/home/pht2/Storage/levita-agent-retirement/`

**Hard ordering invariant:** Tasks 1 through 5 must pass before Task 6 can stop a container. Tasks 6 and 7 must pass before Matrix production deployment. Protected Hermes activation occurs only after local tests and an exact diff/rollback record exist.

---

## Task 1: Create the isolated execution repository and durable evidence model

**Required specialist:** production-triage

**Files:**
- Create: `/home/pht2/levita-matrix-intake/pyproject.toml`
- Create: `/home/pht2/levita-matrix-intake/README.md`
- Create: `/home/pht2/levita-matrix-intake/src/levita_ops/__init__.py`
- Create: `/home/pht2/levita-matrix-intake/src/levita_ops/models.py`
- Create: `/home/pht2/levita-matrix-intake/tests/test_models.py`
- Create: `/home/pht2/levita-matrix-intake/evidence/.gitkeep`

**Step 1: Write failing model tests**

Cover:
- candidate records require container ID, name, image identity, ownership evidence, restart policy, mounts, networks, and inclusion reason;
- ambiguous ownership cannot serialize as `retire_allowed=true`;
- preserved records require an exclusion reason;
- backup gate can be `PASS` only when every required check is true;
- secrets are rejected from public evidence fields by key-name and value-pattern checks.

Core contract:

```python
@dataclass(frozen=True)
class Candidate:
    container_id: str
    name: str
    image_ref: str
    image_id: str
    compose_project: str | None
    compose_service: str | None
    coolify_resource_id: str | None
    ownership_evidence: tuple[str, ...]
    restart_policy: str
    mounts: tuple[MountRecord, ...]
    networks: tuple[str, ...]
    dependency_evidence: tuple[str, ...]
    inclusion_reason: str
    retire_allowed: bool

@dataclass(frozen=True)
class BackupGate:
    ssd_identity_ok: bool
    kernel_delta_clean: bool
    manifest_ok: bool
    archives_ok: bool
    extraction_ok: bool
    pg_dumps_ok: bool
    restore_material_ok: bool
    restore_feasibility_ok: bool

    @property
    def verdict(self) -> str:
        return "PASS" if all(dataclasses.astuple(self)) else "BLOCKED"
```

**Step 2: Run the tests and prove RED**

Run: `uv run --with pytest pytest -q tests/test_models.py`

Expected: FAIL because the models do not exist.

**Step 3: Implement the minimum evidence model and JSON serialization**

Use strict dataclasses or Pydantic-free validation. Keep the execution repo dependency-light. Redact secret-shaped values before writing public evidence.

**Step 4: Run focused and full tests**

Run:

```bash
uv run --with pytest pytest -q tests/test_models.py
uv run --with pytest pytest -q
```

Expected: PASS.

**Step 5: Initialize Git and commit**

```bash
git init
git add pyproject.toml README.md src tests evidence/.gitkeep
git diff --cached --check
git commit -m "chore: scaffold Levita retirement evidence tooling"
```

---

## Task 2: Implement read-only SSD and phantom production triage

**Required specialist:** production-triage

**Files:**
- Create: `/home/pht2/levita-matrix-intake/src/levita_ops/triage.py`
- Create: `/home/pht2/levita-matrix-intake/src/levita_ops/ssh.py`
- Create: `/home/pht2/levita-matrix-intake/tests/test_triage.py`
- Create at runtime: `/home/pht2/levita-matrix-intake/evidence/triage-<UTC>.json`
- Create at runtime: `/home/pht2/levita-matrix-intake/evidence/candidate-matrix-<UTC>.json`
- Create at runtime: `/home/pht2/levita-matrix-intake/evidence/preserved-matrix-<UTC>.json`

**Step 1: Write failing parser and classification tests**

Fixture tests must cover:
- exact block device `/dev/sda1`, UUID, mountpoint, `rw`, parent state `running`, and free-byte threshold;
- fail closed on device alias mismatch, stale mount, read-only mount, parent not running, or new USB/I/O/ext4 error lines;
- Docker inventory parsing from NUL-safe or JSON output;
- candidate inclusion from Compose/Coolify ownership plus dependency evidence, never from name alone;
- Chatwoot, Evolution, Levita storefront, GP, PipesLQD, Coolify, proxy, and unrelated services preserved;
- ambiguous service appears in an `unresolved` set and is not retirement-eligible.

**Step 2: Run tests and prove RED**

Run: `uv run --with pytest pytest -q tests/test_triage.py`

**Step 3: Implement read-only collectors**

Local SSD collector uses `lsblk --json`, `findmnt --json`, `statvfs`, and a bounded kernel-journal baseline. Remote collector uses only `ssh phantom` and read-only commands such as:

```bash
docker ps --no-trunc --format '{{json .}}'
docker inspect <exact IDs>
docker image inspect <exact image IDs>
docker volume inspect <candidate volume names>
docker network inspect <candidate network names>
docker compose ls --format json
docker stats --no-stream --format '{{json .}}'
free -b
df -B1
```

Do not print container environments. Record only environment key names and hashes of secret-bearing files when needed for restore identity.

**Step 4: Execute live triage**

Run:

```bash
uv run python -m levita_ops.triage \
  --host phantom \
  --device /dev/sda1 \
  --uuid 7f2d2fda-510a-4010-9c3c-7982685dc4b3 \
  --mount /home/pht2/Storage \
  --out evidence
```

Expected: exit 0 only when SSD and production prerequisites pass. No mutation is allowed.

**Step 5: Manually reconcile the live candidate matrix**

Require positive ownership and dependency proof for:
- Dify Coolify project/service `o10oybgdcl4uy0wc07m4zqcp` and its exact current containers;
- production bridge `ab66e70db503bde09b8b6092ba`;
- staging bridge `s9ckw1kfoxec0b1ekdvg5l5p-131130209807`;
- standalone Levita helpers listed in the design.

Produce an exact preserved/shared list with health baseline. Anything unresolved remains preserved.

**Step 6: Validate and commit**

```bash
uv run --with pytest pytest -q
git add src tests evidence/*.json
git diff --cached --check
git commit -m "feat: add fail-closed Levita production triage"
```

---

## Task 3: Implement the complete backup builder

**Required specialist:** production-triage

**Files:**
- Create: `/home/pht2/levita-matrix-intake/src/levita_ops/backup.py`
- Create: `/home/pht2/levita-matrix-intake/src/levita_ops/remote_backup.py`
- Create: `/home/pht2/levita-matrix-intake/tests/test_backup.py`
- Create: `/home/pht2/levita-matrix-intake/templates/RESTORE.md.j2`
- Create at runtime: `/home/pht2/Storage/levita-agent-retirement/<UTC>.partial/`
- Atomic final path: `/home/pht2/Storage/levita-agent-retirement/<UTC>/`

**Step 1: Write failing backup-plan tests**

Prove:
- backup refuses to start unless triage evidence is current and SSD qualification matches;
- every candidate mount has one capture strategy;
- every candidate-owned PostgreSQL database has one logical dump command;
- images are deduplicated by image ID and captured by `docker image save`, or represented by an immutable digest with offline restoration proof;
- secret-bearing restoration files are copied with mode `0600` and never emitted in stdout or public JSON;
- finalization cannot rename `.partial` until every required artifact exists;
- source drift during backup causes BLOCKED rather than silent continuation.

**Step 2: Prove RED, then implement**

The builder creates these subtrees:

```text
metadata/
compose/
source/
images/
binds/
volumes/
postgres/
dify/
manifests/
restore/
```

Remote capture should stream archives from `phantom` directly to the SSD where practical. It must not stage multi-gigabyte secret-bearing copies under `/tmp` on either host.

**Step 3: Capture Dify restoration material**

Capture:
- two-app inventory, `Levita PROD` and `Levita Telegram Test`;
- Dify DSL export through supported API/UI only when available without printing tokens;
- workflow/config database logical dump as the authoritative fallback;
- Dify bind tree, compose material, image identities, plugin/storage volumes, and restore order.

**Step 4: Execute the live backup**

Run the builder as a tracked background process with completion notification because it may exceed the conversation turn:

```bash
uv run python -m levita_ops.backup \
  --host phantom \
  --triage evidence/triage-<UTC>.json \
  --candidates evidence/candidate-matrix-<UTC>.json \
  --target /home/pht2/Storage/levita-agent-retirement/<UTC>.partial
```

The command returns a machine-readable summary with path, bytes written, artifact counts, and blocked checks, never secret values.

**Step 5: Generate a restore runbook and preliminary manifest**

The runbook must include prerequisites, image load/pull, network/volume recreation, bind ownership, PostgreSQL roles and database restore, Compose/Coolify recreation, Dify app verification, bridge/helper restart, health checks, and rollback.

**Step 6: Run tests and commit code only**

Do not commit external backup contents. Commit tooling and sanitized evidence references.

---

## Task 4: Implement and execute independent backup verification

**Required specialist:** release-gatekeeper, read-only

**Files:**
- Create: `/home/pht2/levita-matrix-intake/src/levita_ops/verify_backup.py`
- Create: `/home/pht2/levita-matrix-intake/tests/test_verify_backup.py`
- Create in backup: `manifests/SHA256SUMS`
- Create in backup: `manifests/verification.json`
- Create in backup: `restore/RESTORE.md`
- Create locally: `evidence/release-gate-backup-<UTC>.md`

**Step 1: Write failing verifier tests**

Cover checksum mismatch, unreadable tar, missing manifest member, failed representative extraction, PostgreSQL custom dump listing failure, plain SQL parse failure, missing restore prerequisite, insufficient free-space headroom, and new kernel storage error delta.

**Step 2: Implement a verifier that is independent of the backup builder**

The verifier must recompute rather than trust builder summaries. It runs:

```bash
sha256sum -c manifests/SHA256SUMS
tar -tf <every tar archive>
docker image load --input <representative image tar> only in an isolated validation daemon when available
pg_restore --list <every custom-format dump>
psql --set ON_ERROR_STOP=1 --single-transaction against a disposable PostgreSQL matching the dump family when feasible
```

If a full disposable restore is infeasible, the report must state the exact bounded restore feasibility performed and residual risk.

**Step 3: Execute full verification and representative extraction**

Use a separate directory on the SSD or a disposable local filesystem with sufficient space. Requalify `/dev/sda1` and kernel error delta before and after.

**Step 4: Finalize atomically only on PASS**

Rename `<UTC>.partial` to `<UTC>` only after PASS. Store the final path, byte count, manifest hash, artifact counts, and verifier version in `verification.json`.

**Step 5: Independent release verdict**

The release-gatekeeper reports exactly:

```text
Verdict: PASS | BLOCKED
Backup path: <final path or partial path>
Bytes: <exact>
Manifest: <SHA-256>
Checks: manifest, archives, extraction, PostgreSQL, restore material, SSD end state
Blockers: <none or exact evidence>
```

No retirement task may begin without `Verdict: PASS`.

---

## Task 5: Freeze the exact retirement transaction and rollback artifact

**Required specialist:** release-gatekeeper, read-only

**Files:**
- Create: `/home/pht2/levita-matrix-intake/src/levita_ops/retirement_plan.py`
- Create: `/home/pht2/levita-matrix-intake/tests/test_retirement_plan.py`
- Create: `/home/pht2/levita-matrix-intake/evidence/retirement-transaction-<UTC>.json`
- Create: `/home/pht2/levita-matrix-intake/evidence/retirement-gate-<UTC>.md`

**Step 1: Generate the transaction from live IDs and validated backup evidence**

For each retirement candidate freeze:
- full container ID and name;
- image ID/digest;
- Compose project/service and Coolify resource;
- restart policy and autorecreate owner;
- exact stop/remove action;
- exact rollback action and backup artifact;
- preserved-service checks that must remain green after stop.

**Step 2: Re-read live state and fail on drift**

Any container replacement, mount/network change, ownership ambiguity, or backup manifest mismatch blocks the transaction.

**Step 3: Independent PASS/BLOCKED gate**

The gatekeeper checks candidate completeness, shared dependency exclusion, idempotency, post-side-effect recovery, rollback, and no broad container selectors. Only exact IDs are allowed.

---

## Task 6: Selectively retire the proved Levita runtime

**Required specialist:** deploy-operator

**Files:**
- Create: `/home/pht2/levita-matrix-intake/src/levita_ops/retire.py`
- Create: `/home/pht2/levita-matrix-intake/tests/test_retire.py`
- Create: `/home/pht2/levita-matrix-intake/evidence/retirement-result-<UTC>.json`

**Step 1: Write command-rendering and drift tests**

Reject names, globs, project-wide down operations, broad `docker system prune`, volume deletion, and any target not in the signed transaction. Require exact container IDs and preserved-health callbacks.

**Step 2: Disable autorecreate at the owning layer**

Use Coolify or the candidate Compose owner, not ad hoc restart-policy mutation, when the service is managed. Snapshot the previous deploy/autodeploy state first. Do not affect Chatwoot, Evolution, storefront, GP, PipesLQD, Coolify, proxy, or unrelated projects.

**Step 3: Stop exact candidates in dependency-aware groups**

After each group:
- read exact stopped state;
- run preserved/shared health checks;
- compare container identity and health;
- abort and rollback if a preserved service regresses.

**Step 4: Remove exact stopped candidate containers**

Do not remove volumes, bind data, images, networks, or backup material unless the frozen transaction explicitly proves candidate-only ownership and removal is required. Default is to retain data-bearing artifacts after container removal.

**Step 5: Prove no autorecreation and collect resource readback**

Observe across at least two orchestrator polling windows. Record before/after memory, swap, disk, container count, candidate count, and preserved health.

**Step 6: Commit tooling and sanitized result evidence**

---

## Task 7: Build and locally validate the private Matrix stack

**Required specialist:** deploy-operator

**Files:**
- Create: `/home/pht2/levita-matrix-intake/deploy/matrix/compose.yaml`
- Create: `/home/pht2/levita-matrix-intake/deploy/matrix/homeserver.yaml.template`
- Create: `/home/pht2/levita-matrix-intake/deploy/matrix/log.config`
- Create: `/home/pht2/levita-matrix-intake/deploy/matrix/README.md`
- Create: `/home/pht2/levita-matrix-intake/deploy/matrix/backup.py`
- Create: `/home/pht2/levita-matrix-intake/deploy/matrix/validate.py`
- Create: `/home/pht2/levita-matrix-intake/tests/test_matrix_config.py`
- Create: `/home/pht2/levita-matrix-intake/tests/test_matrix_stack.py`

**Step 1: Write policy tests before Compose/config**

Assert:
- Synapse and PostgreSQL images use pinned immutable references;
- registration and registration-without-verification are disabled;
- federation send and federation listeners are disabled;
- public room directory and room-list publication are disabled;
- no public admin UI service exists;
- media limits are explicit;
- secrets are placeholders sourced from protected env files, not literals;
- PostgreSQL and media backup/restore commands exist;
- health checks exist.

**Step 2: Prove RED, then implement the smallest stack**

The stack contains only:
- Synapse;
- PostgreSQL;
- private reverse-proxy binding only if an existing approved private hostname or Tailscale route is proved.

Do not add Element Web, public registration UI, federation proxy, Redis, workers, or monitoring stacks unless live load proves they are necessary.

**Step 3: Run local disposable stack tests**

Validate Compose rendering without secrets in output, health, registration rejection, public directory rejection, federation endpoint rejection, authenticated owner room creation, media upload limits, database backup, media backup, restart, and restore into a disposable stack.

**Step 4: Record rollback state and commit**

---

## Task 8: Deploy Matrix through Coolify or approved private host route

**Required specialist:** deploy-operator

**Files:**
- Create: `/home/pht2/levita-matrix-intake/evidence/matrix-deploy-preflight-<UTC>.json`
- Create: `/home/pht2/levita-matrix-intake/evidence/matrix-deploy-result-<UTC>.json`
- Create: `/home/pht2/levita-matrix-intake/evidence/matrix-rollback-<UTC>.md`

**Step 1: Prove the route before mutation**

Read-only checks establish one of:
- an existing approved private hostname already routed through Coolify/Tailscale;
- a private Tailscale IP/route usable by FluffyChat without DNS mutation.

If neither is safe, open one narrow owner gate for DNS/domain choice. Do not improvise public DNS.

**Step 2: Snapshot rollback and generate credentials without printing them**

Store secrets in Coolify or protected root-owned files. Record only key names, locations, modes, and checksums. Capture previous or empty resource state.

**Step 3: Deploy via normal Coolify path**

Use a custom Compose service or source app with pinned images. Do not run GitHub-hosted Actions. Queue through Coolify and record deployment UUID/status.

**Step 4: Live readback**

Prove exact running image digests, PostgreSQL readiness, Synapse client API health, private reachability, no public registration/directory/federation, media storage, backup success, restart, and rollback procedure.

**Step 5: Create exact owner and intake accounts and one private room**

Create accounts through local admin CLI or API without exposing credentials. Record exact owner MXID, intake MXID, and room ID in protected runtime configuration only. Public evidence contains hashes or redacted suffixes.

---

## Task 9: Implement the Matrix-to-cockpit intake plugin with TDD

**Required specialist:** Hermes runtime maintenance

**Files:**
- Create: `/home/pht2/levita-matrix-intake/plugin.yaml`
- Create: `/home/pht2/levita-matrix-intake/src/levita_matrix_intake/__init__.py`
- Create: `/home/pht2/levita-matrix-intake/src/levita_matrix_intake/contracts.py`
- Create: `/home/pht2/levita-matrix-intake/src/levita_matrix_intake/store.py`
- Create: `/home/pht2/levita-matrix-intake/src/levita_matrix_intake/bundler.py`
- Create: `/home/pht2/levita-matrix-intake/src/levita_matrix_intake/router.py`
- Create: `/home/pht2/levita-matrix-intake/src/levita_matrix_intake/mattermost.py`
- Create: `/home/pht2/levita-matrix-intake/src/levita_matrix_intake/renderer.py`
- Create: `/home/pht2/levita-matrix-intake/tests/test_store.py`
- Create: `/home/pht2/levita-matrix-intake/tests/test_bundler.py`
- Create: `/home/pht2/levita-matrix-intake/tests/test_router.py`
- Create: `/home/pht2/levita-matrix-intake/tests/test_renderer.py`
- Create: `/home/pht2/levita-matrix-intake/tests/test_plugin.py`

**Step 1: Write RED tests for the security boundary**

Tests must reject:
- wrong MXID;
- wrong room ID;
- stale event;
- replayed event ID;
- unbound reply/update/decision;
- cross-task approval;
- edits or bridge ghosts without authoritative binding;
- attempts to invoke production/deploy/customer-send/destructive tools from Matrix.

**Step 2: Write RED tests for bundling and dedupe**

Prove ordered text, voice, image, document, and correction bundling; bounded debounce; original event preservation; canonical bundle key; restart dedupe in SQLite; and exactly-once Mattermost side-effect semantics.

SQLite tables:

```sql
CREATE TABLE matrix_events (
  room_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  sender_mxid TEXT NOT NULL,
  received_at TEXT NOT NULL,
  canonical_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  PRIMARY KEY (room_id, event_id)
);

CREATE TABLE correlations (
  matrix_room_id TEXT NOT NULL,
  matrix_root_event_id TEXT NOT NULL,
  mattermost_source_channel_id TEXT NOT NULL,
  mattermost_source_root_id TEXT NOT NULL,
  mattermost_source_post_id TEXT NOT NULL,
  cockpit_task_id TEXT,
  execution_root_id TEXT,
  canonical_hash TEXT NOT NULL,
  PRIMARY KEY (matrix_room_id, matrix_root_event_id),
  UNIQUE (mattermost_source_post_id)
);
```

**Step 3: Implement the intake contract**

The plugin exposes a narrow tool or hook consumed only by the restricted profile. It validates exact allowlists, persists before side effects, posts a structured owner-authored root to Mattermost `main` through the guarded cockpit helper, and stores the full correlation after readback.

**Step 4: Implement compact return rendering**

Only status, decision gate, blocker, and final result plus source permalink may return to Matrix. Strip technical logs, internal IDs, raw tool output, secrets, and execution chatter.

**Step 5: Run tests, security review, and commit**

```bash
uv run --with pytest pytest -q tests/test_store.py tests/test_bundler.py tests/test_router.py tests/test_renderer.py tests/test_plugin.py
uv run --with pytest pytest -q
git diff --check
git commit -am "feat: add restricted Matrix cockpit intake"
```

---

## Task 10: Prepare and apply the protected Hermes profile/runtime diff

**Required specialist:** Hermes runtime maintenance

**Protected files:**
- Create or update: `/home/pht2/.hermes/profiles/levita-matrix-intake/config.yaml`
- Create or update: `/home/pht2/.hermes/profiles/levita-matrix-intake/.env`
- Create or install: `/home/pht2/.hermes/profiles/levita-matrix-intake/plugins/levita-matrix-intake/`
- Create or update: exact user systemd unit for the profile gateway, only if the normal profile gateway installer cannot isolate it safely

**Step 1: Inspect authoritative current docs and CLI**

Verify commands with the current Hermes docs and local `hermes --help`. Use `hermes profile create levita-matrix-intake` only after exact behavior is read back. Do not clone unrelated memories, skills, sessions, platform credentials, or tool access.

**Step 2: Build the exact diff and rollback package before activation**

Back up the target profile path, plugin bytes, unit definitions, and current gateway state. Render a secret-redacted diff that proves:
- only Matrix and the narrow intake/cockpit toolset are enabled;
- exact owner MXID and room ID allowlists;
- Mattermost source `main` binding and owner-authenticated helper path;
- no SSH, terminal, file-write outside plugin state, deploy, browser, customer-send, billing, fiscal, memory, cron, or broad messaging toolsets;
- dedicated state paths and logs;
- no cross-company context or memories.

The owner's original current-chat authorization explicitly covers this protected integration. No duplicate approval prompt is needed unless the diff expands beyond this contract.

**Step 3: Apply with profile-aware commands**

Prefer supported `hermes --profile levita-matrix-intake` and plugin/profile commands. Never switch the global sticky profile. Validate with `hermes --profile levita-matrix-intake config check`, plugin listing, effective toolset listing, and dry-run loader reads without printing secrets.

**Step 4: Start/restart only the dedicated profile gateway**

Do not restart the owner-facing default Gateway. If activation requires a shared default Gateway restart, use detached zero-agent maintenance and preserve active cockpit tasks. Otherwise run an isolated profile unit.

**Step 5: Validate runtime identity and isolation**

Prove exact profile home, platform adapter, room/user allowlists, plugin registration, database path, and absence of unrelated company data/toolsets.

---

## Task 11: Execute independent Matrix, cockpit, and owner-device validation

**Required specialist:** independent validator, separate from deploy operator

**Files:**
- Create: `/home/pht2/levita-matrix-intake/tests/e2e/test_matrix_cockpit_contract.py`
- Create: `/home/pht2/levita-matrix-intake/evidence/live-validation-<UTC>.md`
- Create: `/home/pht2/levita-matrix-intake/evidence/owner-device-checklist-<UTC>.md`

**Step 1: Automated negative-path validation**

Prove:
- registration denied;
- public directory denied;
- federation denied;
- wrong user denied;
- wrong room denied;
- stale/replayed event denied;
- unknown reply or approval denied;
- no Mattermost post or cockpit task side effect on rejection.

**Step 2: Automated restart/dedupe validation**

Send one synthetic allowed event, stop and restart the restricted profile after persistence and at multiple side-effect boundaries, replay the event, and prove one Matrix ledger record, one owner-authored Mattermost source root, one cockpit task, and one `Execuções` root.

**Step 3: Real owner-device validation in FluffyChat**

The owner must exercise on the phone:
1. text burst;
2. voice note with preserved original and transcription;
3. image;
4. mixed voice, image, and correction bundle;
5. one executable task creating exactly one source root and one cockpit task;
6. one draft that does not execute;
7. one update bound to the exact existing source thread;
8. compact status and final return;
9. device restart/re-login and no duplicate routing.

Do not send to customers or use live WhatsApp/Chatwoot recipients.

**Step 4: E2EE phase only after plaintext contract passes**

Enable E2EE, then repeat encrypted text, voice, image, reply context, key backup, and device recovery. If FluffyChat push uses FCM/APNs, disclose that metadata boundary accurately.

**Step 5: Independent final verdict**

Report PASS/BLOCKED for backup, retirement, Matrix server, privacy, Hermes isolation, cockpit correlation, owner-device flows, restart dedupe, and rollback.

---

## Task 12: Final repository review, evidence publication, and cockpit closeout

**Required specialists:** release-gatekeeper and cockpit coordinator

**Files:**
- Update: `/home/pht2/levita-matrix-intake/README.md`
- Create: `/home/pht2/levita-matrix-intake/evidence/FINAL-<UTC>.md`
- Update cockpit durable task state only through `hermes-mattermost-cockpit`

**Step 1: Independent code and release review**

Review exact baseline and candidate SHAs, diff, tests, security boundary, side-effect idempotency, rollback, and live evidence. Patch blockers, rerun the full relevant battery, and obtain a final PASS.

**Step 2: Commit final artifacts without secrets**

```bash
git status --short
git diff --check
uv run --with pytest pytest -q
git add README.md src tests deploy plugin.yaml evidence/*.md evidence/*.json
git diff --cached --check
git commit -m "docs: finalize Levita retirement and Matrix evidence"
```

Do not push to a remote unless a suitable private repository exists and publication does not trigger paid GitHub-hosted Actions.

**Step 3: Publish detailed evidence in the bound `Execuções` root**

Include:
- final backup path, exact bytes, manifest SHA-256, archive/DB/restore checks;
- exact retired and preserved lists;
- before/after resource pressure;
- Matrix image digests, health and privacy readbacks;
- Hermes profile/plugin diff hash and rollback path;
- owner-device and restart-dedupe results;
- remaining risks or none.

**Step 4: Close cockpit in the mandatory order**

1. validate terminal evidence;
2. prove the execution root remains unfollowed;
3. stop the exact watcher and prove unit inactive, PID zero, and no exact process;
4. prove durable watcher owner and heartbeat are null;
5. post one compact final result with execution permalink to the source `main` thread;
6. read the relay back;
7. mark the durable task `SUCCEEDED` only after every cleanup and relay check passes.

**Step 5: Owner-facing completion format**

```text
Done: verified backup, exact retirement result, private Matrix status.
Evidence: backup bytes/manifest, retired count, memory delta, Matrix/privacy checks, owner-device matrix.
Pending/blocker: none, or one exact held gate.
Approval: none needed, or the one exact new high-impact decision.
```
