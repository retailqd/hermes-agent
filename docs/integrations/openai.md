# OpenAI integration contract

- Last verified: 2026-08-28
- Status: live-verified
- Package/SDK: `openai==2.24.0`
- Provider API: Responses API through the Codex OAuth backend
- Runtime/config surface: `agent/conversation_loop.py`, `agent/message_content.py`, `agent/codex_responses_adapter.py`, `tools/image_source.py`

## Primary sources

- OpenAI Responses API reference: <https://developers.openai.com/api/reference/cli/resources/responses/methods/create>
- OpenAI model catalog: <https://developers.openai.com/api/docs/models>
- Installed OpenAI Python SDK `2.24.0` and the Hermes adapter source listed above

## Contract facts used

- A user message may contain structured text and `input_image` parts; wrappers around a turn must preserve every non-text part.
- Native inline image input uses either a data URL or a provider-supported image URL and must not be flattened into a textual Python representation.
- A malformed or truncated inline image must be rejected before it is persisted or sent back to the provider on later turns.
- Plan Mode control text is inserted by replacing only the textual portion of a multimodal message.

## Verification

| Command or probe | Sanitized result |
|---|---|
| `pytest -q tests/hermes_cli/test_plan_mode.py tests/tools/test_vision_native_fast_path.py` | pass, 55 tests |
| Real Aztecs Plan Mode image prompt through AoE | pass: a persisted 26,260-byte PNG produced `IMAGE_OK:42` and `prompt_complete`, with no HTTP 400 |
| AoE attachment replay | pass: the authenticated attachment download returned `image/png` and matched the submitted file's SHA-256 |

## Risks and gaps

- Large base64 inputs still count toward transport and provider limits even though they no longer contaminate the text transcript.
- URLs fetched by the vision tool remain subject to its size, redirect, and content-type policy.
