# Task 8 Report

## Goal
Preserve Mattermost cockpit idempotency metadata in `post.props` so owner-facing messages never expose `[cockpit-relay:*]`, `[cockpit-gate:*]`, or `[cockpit-final:*]` markers in the body, while edits keep the hidden metadata intact.

## Root cause found
- `MattermostClient.update_post()` only accepted `message` and sent `{id, message}`.
- `CockpitService.relay_status()` edited existing status posts with only the new message, which would drop metadata if the API treated update payloads as a full replacement.
- Status relay posts rely on `props` to keep internal markers hidden from the message body.

## Fix applied
- Added optional `props` support to `MattermostClient.update_post()` and serialize it into the update payload.
- Updated `CockpitService.relay_status()` to fetch the current post before editing and resend its `props` on update.
- Kept the edit path minimal and limited to the status relay branch.

## Tests added/updated
- Added client test proving `update_post(..., props=...)` includes `props` in the request body.
- Added service assertion proving `relay_status()` preserves the existing relay props on edit.
- Updated the service fake client to capture and apply props on updates.

## Verification
- `pytest -q tests/hermes_cli/test_mattermost_cockpit_client.py tests/hermes_cli/test_mattermost_cockpit_service.py -k "props or marker or relay_status"`
- `pytest -q tests/hermes_cli/test_mattermost_cockpit_client.py tests/hermes_cli/test_mattermost_cockpit_service.py`

## Files changed
- `hermes_cli/mattermost_cockpit/client.py`
- `hermes_cli/mattermost_cockpit/service.py`
- `tests/hermes_cli/test_mattermost_cockpit_client.py`
- `tests/hermes_cli/test_mattermost_cockpit_service.py`

## Concerns / follow-ups
- If Mattermost ever starts returning non-mapping `props` on existing posts, the service now skips re-sending them rather than failing.
- No broader relays were changed; only the status relay edit path was adjusted because that was the proven gap.
