# Mattermost Cockpit Main Readability Activation Plan

Status: **PREPARED, NOT APPLIED**

Date: 2026-07-24

## Purpose

Activate the reviewed Mattermost Cockpit readability change without losing unrelated local work, without modifying protected runtime surfaces before explicit owner approval, and without restarting while agent work is active.

## Immutable release inputs

- Feature candidate: `da830ab52b78447bc3d7ddf6eca0e5d6083aa869`
- Candidate integrated over the currently active checkout: `b5992f6ce05e7e7ab09c1131d3d2e85b987eaf9d`
- Required active checkout before activation: `5db8c397c6de6274d1b92ce4f184ebbe2002fe7f`
- Code rollback target: `5db8c397c6de6274d1b92ce4f184ebbe2002fe7f`
- Protected policy patch: `/home/pht2/mc-semantic-local-safe/docs/superpowers/activation/2026-07-24-cockpit-main-readability-policy.patch`
- Protected policy patch SHA-256: `7b386369df4eceb62d315ac26db62487292b153723ae7ebb8f1cad37198e1dbe`
- Active code checkout: `/home/pht2/.hermes/hermes-agent`
- Cockpit database: `/home/pht2/.hermes/mattermost-cockpit/state.db`
- Gateway/session database: `/home/pht2/.hermes/state.db`

The integrated candidate deliberately descends from the active checkout and preserves its unrelated local documentation commits. Activation uses `--ff-only`, never `reset`, cherry-pick, or force checkout.

## Invalidation conditions

Abort before any mutation if any of these is true:

1. Explicit owner approval for protected policy writes and Gateway restart is absent in the current thread.
2. `/home/pht2/.hermes/hermes-agent` is not clean.
3. Its `HEAD` is not exactly `5db8c397c6de6274d1b92ce4f184ebbe2002fe7f`.
4. `git merge-base --is-ancestor 5db8c397c... 1f20a92d...` fails.
5. The policy patch SHA-256 differs from the pinned value.
6. The candidate cannot pass the Cockpit suite, compile, and pyright from an isolated worktree.
7. Any active agent, open Cockpit execution, Cockpit watcher unit, or non-Gateway process remains in the Gateway cgroup.
8. SQLite integrity or consistent backup fails.
9. A new task starts between the final drain check and the Gateway stop.

If the active checkout changes, build, review, validate, and pin a new integrated candidate. Do not reuse this plan by substituting a SHA manually.

## Phase 1: explicit approval gate

The approval request must name all protected actions:

- apply the reviewed patch to `/home/pht2/.hermes/SOUL.md` and `/home/pht2/.hermes/AGENTS.md`;
- fast-forward the active code checkout to the pinned integrated candidate;
- restart `hermes-gateway.service` from a detached user-systemd runner;
- execute the dedicated live Mattermost smoke;
- roll back automatically if health or smoke validation fails.

No protected write, restart, systemd mutation, or live smoke occurs before that approval.

## Phase 2: immutable preflight

Run read-only checks:

```bash
set -euo pipefail
LIVE=/home/pht2/.hermes/hermes-agent
CANDIDATE=b5992f6ce05e7e7ab09c1131d3d2e85b987eaf9d
ROLLBACK=5db8c397c6de6274d1b92ce4f184ebbe2002fe7f
PATCH=/home/pht2/mc-semantic-local-safe/docs/superpowers/activation/2026-07-24-cockpit-main-readability-policy.patch

test "$(git -C "$LIVE" rev-parse HEAD)" = "$ROLLBACK"
test -z "$(git -C "$LIVE" status --porcelain)"
git -C "$LIVE" cat-file -e "$CANDIDATE^{commit}"
git -C "$LIVE" merge-base --is-ancestor "$ROLLBACK" "$CANDIDATE"
test "$(sha256sum "$PATCH" | cut -d' ' -f1)" = \
  7b386369df4eceb62d315ac26db62487292b153723ae7ebb8f1cad37198e1dbe
patch --dry-run --batch --forward -p1 -d /home/pht2 < "$PATCH"
```

Revalidate the exact candidate from an isolated worktree:

```bash
/home/pht2/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/hermes_cli/test_mattermost_cockpit_*.py -q
/home/pht2/.hermes/hermes-agent/venv/bin/python -m compileall -q \
  hermes_cli/mattermost_cockpit
uvx pyright hermes_cli/mattermost_cockpit
```

Expected baseline for the pinned candidate:

- `154 passed`
- compile exit code `0`
- pyright `0 errors, 0 warnings, 0 informations`

## Phase 3: durable backups

Create backups before policy or code mutation. Use SQLite's online backup API, not raw file copying.

```bash
set -euo pipefail
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="/home/pht2/.hermes/backups/cockpit-main-readability-$STAMP"
install -d -m 700 "$BACKUP"

python3 - "$BACKUP" <<'PY'
import sqlite3
import sys
from pathlib import Path

out = Path(sys.argv[1])
for source, name in (
    (Path('/home/pht2/.hermes/state.db'), 'state.db'),
    (Path('/home/pht2/.hermes/mattermost-cockpit/state.db'), 'mattermost-cockpit-state.db'),
):
    src = sqlite3.connect(f'file:{source}?mode=ro', uri=True)
    dst = sqlite3.connect(out / name)
    try:
        src.backup(dst)
        result = dst.execute('PRAGMA integrity_check').fetchone()[0]
        if result != 'ok':
            raise SystemExit(f'{name}: integrity_check={result!r}')
    finally:
        dst.close()
        src.close()
PY

install -m 600 /home/pht2/.hermes/SOUL.md "$BACKUP/SOUL.md"
install -m 600 /home/pht2/.hermes/AGENTS.md "$BACKUP/AGENTS.md"
printf '%s\n' 5db8c397c6de6274d1b92ce4f184ebbe2002fe7f > "$BACKUP/code-rollback-sha"
sha256sum "$BACKUP"/* > "$BACKUP/SHA256SUMS"
```

Record `$BACKUP` in the detached runner log before proceeding.

## Phase 4: drain and zero-work gate

Do not restart merely because the candidate is ready. Wait until the runtime is idle.

Required observations:

1. `/home/pht2/.hermes/active_agents.json` reports zero active agents.
2. The Cockpit store has zero nonterminal tasks.
3. `systemctl --user list-units 'hermes-mattermost-cockpit@*.service' --state=active --no-legend` is empty.
4. The Gateway cgroup contains only the Gateway main PID, with no child agent process.
5. All four conditions remain true for two checks 30 seconds apart.
6. Immediately before `systemctl --user stop hermes-gateway.service`, repeat all checks in the same detached runner. Abort if work reappears.

Read-only Cockpit query:

```sql
SELECT task_id, lifecycle, cleanup_state
FROM tasks
WHERE lifecycle NOT IN ('SUCCEEDED', 'FAILED', 'CANCELLED');
```

The activation window must be announced in the owner thread so no new work is submitted during the final drain and restart interval.

## Phase 5: detached activation runner

The activation must run in a transient user-systemd unit outside `hermes-gateway.service`. Never restart the Gateway from the foreground conversation process.

Launch shape after approval:

```bash
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
systemd-run --user --no-block --collect \
  --unit="hermes-cockpit-readability-release-$STAMP" \
  --property=Type=oneshot \
  /usr/bin/bash /home/pht2/.hermes/releases/cockpit-main-readability/activate.sh
```

The approved `activate.sh` must:

1. acquire an exclusive release lock;
2. repeat immutable preflight and the zero-work gate;
3. create and verify backups;
4. stop the Gateway;
5. prove the Gateway cgroup is empty;
6. apply the protected policy patch from `/home/pht2`, first with `patch --dry-run --batch --forward -p1`, then with the same command without `--dry-run`;
7. fast-forward code only:

   ```bash
   git -C /home/pht2/.hermes/hermes-agent merge --ff-only \
     b5992f6ce05e7e7ab09c1131d3d2e85b987eaf9d
   ```

8. verify the resulting exact SHA and clean checkout;
9. start the Gateway;
10. validate service, bridge, SQLite counts/integrity, and Mattermost connectivity;
11. execute the live smoke;
12. roll back automatically on any failure;
13. write a final success/failure record and release the lock.

The runner script itself must be shown and approved with the protected diff before launch. Creating or launching it is an activation action, not preparation.

## Phase 6: post-restart health validation

Require all checks, not merely process exit:

```bash
systemctl --user is-active hermes-gateway.service
systemctl --user show hermes-gateway.service \
  -p ActiveState -p SubState -p MainPID -p ControlGroup
hermes gateway status
```

Then verify:

- Gateway journal has no startup exception, auth failure, bridge disconnect, or repeated restart;
- Mattermost bridge is connected;
- `/home/pht2/.hermes/state.db` and Cockpit DB pass `PRAGMA integrity_check`;
- pre/post session and message counts are not lower unexpectedly;
- active checkout is the exact candidate SHA and clean;
- protected files match the approved patch result;
- no watcher unit was orphaned by the restart.

## Phase 7: dedicated live Mattermost smoke

Use one new owner-authored top-level root in `main`, clearly labeled as an activation smoke. Bind exactly one Cockpit task to that source root. Do not reuse a production task or an existing execution root.

Exercise the complete visible flow:

1. **Create**: source thread receives one compact `Em andamento` relay with exactly one technical permalink as the final element.
2. **Noise rejection**: post a raw technical diagnostic in the execution root without an approved semantic marker; confirm no source relay appears.
3. **Semantic update**: post one marked owner-safe update; confirm one compact human relay and no raw stdout, IDs, hashes, commands, gates, watcher terms, or extra links.
4. **Gate**: open a gate with exactly `Bloqueado` and `Preciso de você`; confirm one compact decision request.
5. **Decision**: owner replies in the bound source thread; confirm the decision reaches only the bound execution root and duplicate processing remains idempotent.
6. **Close**: close as `SUCCEEDED` with real evidence. Confirm the source final is exactly once, is the last public source post, and has this visible order:
   - `Concluído`
   - `Linguagem leiga`, explaining what was requested, what was done, and current result
   - `Pendências`
   - literal `Nenhuma pendência`
   - exact sentence `Esta execução foi encerrada.`
   - exactly one technical permalink as the final element
7. **Cleanup**: confirm watcher stopped, owner is not following the execution root, no active Cockpit unit remains, and the preserved execution history is readable.
8. **Replay**: retry close and decision operations; confirm no duplicate public relay or final post.
9. **Browser validation**: inspect the actual `main` thread on desktop and mobile width. Confirm formatting and inspect browser console/network for errors.

If any smoke assertion fails, do not mark activation successful. Trigger rollback.

## Phase 8: rollback

Rollback is automatic for failed health or live smoke.

With the Gateway stopped by the detached runner:

1. restore the active checkout only if it still equals the pinned candidate and is clean:

   ```bash
   git -C /home/pht2/.hermes/hermes-agent reset --hard \
     5db8c397c6de6274d1b92ce4f184ebbe2002fe7f
   ```

2. restore `/home/pht2/.hermes/SOUL.md` and `/home/pht2/.hermes/AGENTS.md` from the recorded backup;
3. start the Gateway;
4. repeat service, bridge, DB integrity/count, cgroup, and Mattermost connectivity checks;
5. post a compact owner-facing rollback result with evidence.

Do **not** restore runtime databases by default. The change has no schema migration, and restoring DB backups would discard valid messages created after backup. Restore a DB only if integrity validation proves corruption, after a separate explicit owner decision describing the data-loss window.

## Success criteria

Activation is complete only when:

- exact candidate is active;
- approved policy diff is active;
- Gateway and Mattermost bridge are healthy;
- SQLite integrity and counts are valid;
- full live smoke passes;
- final close is human-readable, exactly once, and last;
- no active or orphan watcher remains;
- rollback evidence is available;
- owner receives a compact evidence-backed closeout.

Until explicit owner approval, this document and the policy patch are preparation artifacts only.
