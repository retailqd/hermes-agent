# Levita Agent Retirement and Private Matrix Intake Design

Date: 2026-07-24
Status: Owner-approved execution design
Company boundary: LQD Art / Levita only
Cockpit task: `levita-agent-retire-matrix-20260724`

## Objective

Retire the unused Levita sales-agent runtime on `phantom` only after a complete and verified backup to the owner's external SSD. Then deploy a lean private Matrix intake for FluffyChat and connect it to the existing Mattermost `main` and `Execuções` cockpit without creating a second execution plane.

## Approved scope and held gates

The owner has approved the ordinary technical lifecycle for this objective, including read-only discovery, backup, scoped container stop and removal, Coolify release actions, Matrix deployment, protected Hermes integration, rollback, and live validation.

The following remain held gates when evidence cannot establish a safe existing choice:

- a new DNS name or domain change;
- creation, rotation, or disclosure of credentials outside the approved deployment bootstrap;
- destructive or irreversible data changes outside the explicitly identified retired runtime;
- any customer, supplier, fiscal, money, shipping, or bulk-visible operation.

Secret values must never be printed in chat, logs, manifests, or validation output.

## Constraints and invariants

1. No candidate container may be stopped or removed before the external backup is complete and all backup gates pass.
2. The backup target must be `/dev/sda1`, UUID `7f2d2fda-510a-4010-9c3c-7982685dc4b3`, mounted read-write at `/home/pht2/Storage`, with parent device state `running` and no new USB, I/O, or ext4 errors.
3. Chatwoot is shared and excluded. Levita storefront, shared Evolution and Chatwoot, General Pipes, PipesLQD, Coolify, proxy, and unrelated services must remain untouched.
4. Candidate membership is based on Compose project, Coolify service ownership, mounts, networks, labels, environment-key names, image provenance, and dependency evidence. A name containing `levita` is not sufficient.
5. Matrix remains an intake and relay. Mattermost `main` remains the canonical owner decision surface. Exactly one cockpit-bound `Execuções` root remains the technical execution surface.
6. The Matrix-side Hermes identity may interpret input, preserve media, correlate events, post owner-authored structured intake to `main`, and return compact statuses. It must not hold production SSH, database, deploy, customer-send, billing, fiscal, or destructive capabilities.
7. Public registration, public room discovery, and federation are disabled. Exact Matrix user and room allowlists are mandatory.
8. E2EE is enabled only after encrypted text, media, replies, device recovery, and key backup pass on the owner's real device.

## Context verified before design freeze

The external SSD is currently visible as `/dev/sda1` with the required UUID, ext4 filesystem, read-write mount at `/home/pht2/Storage`, USB parent state `running`, and about 56.4 GiB available. No matching recent kernel storage error was returned by the read-only check.

`phantom` currently has about 11 GiB RAM, about 2.6 GiB available memory, and exhausted swap. The known candidate names are still running, including the 13-container Dify stack, production and staging bridges, and standalone Levita agent helpers. This is discovery evidence only, not final candidate authorization.

The cockpit task is bound to one owner-authored execution root, is `RUNNING`, and has a fresh task-scoped watcher heartbeat.

## Approaches considered

### Approach A: Archive everything first, then retire by proved ownership, then deploy Matrix

Build an auditable backup bundle from immutable inventory and logical exports. Validate the bundle independently. Freeze and retire only the proved candidate set. Read back preserved services and resource pressure. Deploy Matrix only after the retirement state is stable.

Benefits:

- strongest rollback and isolation guarantees;
- simplest proof that no stop preceded backup validation;
- clear pre-retirement and post-retirement baselines;
- Matrix rollout failures cannot contaminate the retirement evidence.

Trade-off:

- longest elapsed time because phases are serialized at safety boundaries.

### Approach B: Prepare Matrix in parallel with the backup, activate after retirement

Build Matrix artifacts and local tests while the remote backup runs, but do not deploy or activate until backup validation and retirement complete.

Benefits:

- lower elapsed time;
- no extra production mutation before the backup gate.

Trade-off:

- more coordination complexity and a higher chance of mixing evidence or repository ownership.

### Approach C: Deploy Matrix first, then retire the old runtime

This gives the fastest channel replacement but creates additional live services while the VPS is already memory constrained and before the old runtime has a verified rollback bundle.

Benefits:

- early client onboarding.

Trade-off:

- unacceptable memory and rollback risk;
- weakens the required ordering and incident containment.

## Selected approach

Use Approach A as the control flow. Approach B is allowed only for non-mutating local preparation whose artifacts and ownership are clearly isolated. Approach C is rejected.

## Phase 1: Production triage and candidate proof

The production-triage specialist performs read-only discovery first.

Required outputs:

- live device and filesystem baseline for the external SSD;
- live VPS memory, swap, disk, Docker, and Coolify state;
- exact candidate matrix with container ID, name, image digest or immutable pull reference, Compose project and service, Coolify IDs, restart policy, mounts, volumes, networks, ports, labels, dependencies, and reason for inclusion;
- exact preserved/shared matrix with the same identity evidence and reason for exclusion;
- dependency graph proving candidates do not provide required services to preserved workloads;
- size forecast and backup destination with enough safe headroom.

The triage phase is read-only. Ambiguous ownership excludes the service from retirement until resolved.

## Phase 2: Backup construction and release gate

Create a timestamped backup directory under `/home/pht2/Storage` dedicated to this task. Use same-filesystem temporary paths and atomic finalization where practical.

The bundle must include:

- container inspect, image inspect, network inspect, volume inspect, Compose/Coolify ownership metadata, and sanitized inventory;
- Compose and source restoration material, including secret-bearing files copied with restrictive permissions but never printed;
- unique images exported with `docker image save`, or exact immutable pull references when export is unnecessary and offline restore feasibility is proved;
- bind mount contents with ownership, modes, ACL or xattr preservation where applicable;
- named volume contents captured consistently;
- PostgreSQL logical dumps for every candidate-owned database, plus cluster roles or schema restoration metadata as needed;
- Dify application inventory and DSL exports when supported, with a documented fallback if the API does not expose an export;
- a restore runbook with prerequisites, order, commands, validation, and rollback.

The release-gatekeeper must independently verify:

- expected byte counts and free-space headroom;
- SHA-256 manifest generation and full verification;
- archive readability tests;
- representative extraction into a separate temporary directory;
- PostgreSQL dump listing or parsing with the matching client family;
- presence and readability of restoration-critical files;
- basic restore feasibility without touching live services;
- SSD identity, mount state, parent state, and absence of new storage errors at the end of the backup.

Only an explicit PASS permits retirement.

## Phase 3: Selective retirement

Before stopping anything, re-read the live candidate IDs, labels, ownership, dependencies, and backup gate evidence. Fail closed on drift.

Retirement sequence:

1. disable or remove only the candidate-owned autorecreate path in Coolify or Compose;
2. stop only the exact proved candidate containers;
3. validate preserved/shared service health and container identity;
4. remove only the exact stopped candidate containers and candidate-only runtime definitions needed to prevent recreation;
5. retain backup data, source restoration material, and rollback references;
6. observe for autorecreation during a bounded interval;
7. read back memory, swap, disk, container count, preserved services, and external SSD integrity.

If a preserved dependency regresses, stop the phase and restore the affected candidate from the verified bundle or immutable source.

## Phase 4: Private Matrix deployment

Deploy Synapse and PostgreSQL through Coolify or the established host-native deployment path, using immutable image digests or pinned versions. Record the previous or empty rollback state and all non-secret deployment inputs.

Deployment controls:

- registration disabled;
- federation listener and outbound federation disabled or blocked;
- public room directory disabled;
- local private room only;
- separate owner and Hermes intake accounts;
- media size and retention limits sized for owner voice notes and images;
- PostgreSQL and media backups configured and tested;
- health probes for Synapse client API and PostgreSQL;
- no public admin surface;
- no DNS mutation unless a safe existing approved private hostname is proved. Otherwise hold the DNS gate and keep the deployment reachable only through an approved private route.

Credentials are generated or supplied without exposing values. Their storage location, ownership, permissions, rotation procedure, and rollback are documented without secret content.

## Phase 5: Restricted Hermes and cockpit integration

Use a separate restricted Matrix intake identity or profile. The protected Hermes change must be represented as an exact diff and rollback plan before activation, then applied under the owner's explicit current-chat approval.

Intake contract:

1. accept only the exact owner MXID and exact private room ID;
2. debounce short text, voice, image, and document bursts into one ordered bundle;
3. preserve original Matrix event IDs and media;
4. classify bundles as new task, bound update, owner decision, draft, or ambiguous input;
5. create one structured owner-authored `main` post for an executable new task;
6. route updates and decisions only through durable Matrix-to-Mattermost correlation, never by recency;
7. allow the existing cockpit to create or reuse exactly one `Execuções` root;
8. mirror only compact status, gates, blockers, and final results to Matrix;
9. deduplicate by event ID and canonical bundle key across restarts;
10. reject unknown users, rooms, stale events, replayed events, bridge ghosts, unbound edits, and ambiguous cross-task approvals.

Durable correlation includes Matrix room ID, event ID, reply or thread root, Mattermost source channel/root/post, cockpit task ID, and execution root ID.

## Error handling and rollback

- Any SSD identity or health drift blocks backup writes and all retirement actions.
- Any incomplete backup verification blocks retirement.
- Any ownership ambiguity removes the item from the retirement set.
- Any preserved-service regression triggers stop and scoped rollback before continuing.
- Matrix deploy failures roll back to the previous deployment state without reactivating the retired agent unless the retirement itself caused a preserved-service regression.
- Hermes integration failures revert only the exact protected diff and preserve Matrix for isolated diagnosis if safe.
- Unknown or ambiguous Matrix input is rejected or clarified, never executed.
- Technical evidence stays in the bound execution root. Only compact gates, status, and final result go to `main` and Matrix.

## Validation plan

### Backup and retirement

- verify SSD device, UUID, mount options, free bytes, parent state, and kernel error delta before and after backup;
- verify the full SHA-256 manifest;
- test every archive and perform representative extraction;
- list or parse every PostgreSQL dump;
- exercise the restore runbook far enough to prove required artifacts and commands are coherent;
- record the exact retired and preserved lists;
- prove no candidate autorecreates;
- compare VPS memory, swap, disk, and service health before and after;
- preserve the backup path and immutable manifest.

### Matrix server

- health and authenticated client API checks;
- registration rejection;
- public directory rejection;
- federation rejection from an external or independent check;
- unknown-user and unknown-room rejection;
- database and media backup test;
- restart and rollback test;
- pinned image digest readback.

### Owner-device route

On the owner's real FluffyChat device, validate:

1. text burst bundling;
2. voice note, transcription, and preserved original;
3. image attachment and vision path;
4. mixed voice, image, and correction ordering;
5. one command producing exactly one owner-authored `main` root and one cockpit task;
6. one draft that does not execute;
7. one update entering the exact existing `main` thread;
8. one unknown-user or unknown-room rejection;
9. compact status and final return without technical logs;
10. restart recovery with no duplicate Mattermost post or execution root.

Enable and validate E2EE only after the unencrypted routing contract passes. Then repeat encrypted text, voice, image, reply context, key backup, and device recovery tests.

## Completion contract

The task succeeds only when:

- the verified external backup, byte count, SHA-256 manifest, archive and database checks, and restore runbook exist;
- the exact retired and preserved lists are recorded;
- retired services no longer consume RAM or autorecreate;
- preserved services remain healthy;
- Matrix and PostgreSQL are healthy on pinned immutable versions with rollback evidence;
- privacy controls and allowlists are read back live;
- the owner-device validation matrix passes, including restart dedupe and rejection tests;
- the source `main` thread receives a compact evidence-backed result and execution permalink;
- the task watcher is stopped, its durable lease is cleared, and the execution root remains preserved and unfollowed.

If owner-device action, DNS, or credentials become the only remaining blocker, the cockpit opens one narrow plain-language gate and keeps all completed evidence intact.