# CLAUDE.md — Hermes Gateway fork (birk-local)

Guidance for AI agents working in this repo. Keep it terse and accurate.

## Known Issue: Telegram Model Badge

**The feature.** Every outbound Telegram message is prefixed with a one-line
badge: `[🤖 <short-model> · ⚡ <effort> · 🔒 local / ☁️ cloud]`, with
`· switched from <X>` appended ONLY on the turn the model actually changed.
Spec owner (Birk) has confirmed repeatedly: **the badge appears on EVERY
message, not just on a model switch.**

**Why this keeps breaking.** The badge is computed once per turn in
`gateway/run.py` and stashed in the stream metadata as `model_badge`. It then
has to be prepended onto the outbound text at *every* place that puts bytes on
screen. Telegram delivery in `gateway/stream_consumer.py` has **many** distinct
send call sites (first send, progressive edit, overflow-split chunks,
fresh-final resend, fallback continuation, segment-tail flush, draft frames,
interim commentary). Historically the prepend was **scattered** across those
sites, so every newly-added or previously-overlooked send path was a fresh
chance to silently drop the badge. That is the root anti-pattern.

### Design decision: ONE choke point, not N scattered guards

All user-visible platform sends in `GatewayStreamConsumer` now funnel through a
single method **`_adapter_send()`** (`gateway/stream_consumer.py` ~L254), which
is the *only* code that calls `self.adapter.send(...)`. It stamps the badge onto
the **first user-visible message of the turn** (gate:
`_message_id is None and not _already_sent`) via the idempotent helper
**`_apply_model_badge()`** (~L241, `startswith` guard, never double-stamps).

- Continuation fragments (a message is already on screen) are intentionally left
  un-badged — the badge belongs at the top of the turn's first bubble, not
  repeated on every split fragment.
- Full-content rewrites (progressive **edits** and **fresh-final**) arrive
  already badged from `_send_or_edit` (which prepends via `_apply_model_badge`
  before delegating, because edits go through `adapter.edit_message`, **not**
  `_adapter_send`) and pass through the idempotent guard untouched.
- **Draft frames** are the one exception that cannot use `_adapter_send`: native
  draft streaming calls `adapter.send_draft(...)`, a different transport. So
  `_send_draft_frame` applies the *same* first-visible-message gate +
  `_apply_model_badge` itself (~L1215). Same policy, same idempotent guard —
  just enforced at that site because the choke point wraps `adapter.send` only.

**If you add a new send path, call `self._adapter_send(...)`, never
`self.adapter.send(...)` directly.** That is the whole point of the choke point.
Do not reintroduce per-site badge prepends.

### Failure modes (all fixed — do not re-discover)

| # | Symptom | File / anchor | Fix |
|---|---------|---------------|-----|
| 1 | Badge only appeared on the turn the model *switched*, missing otherwise. | `gateway/run.py::_model_badge` (~L1464) | `_model_badge` always returns a badge; the `switched from X` hint is the only switch-gated part (state in `_badge_last_model_by_session`, run.py ~L3208). |
| 2 | MarkdownV2 edit-timeout resend (`run.py` legacy/fallback path) bypassed the streaming badge → badge-less on long / image-bearing replies. | `gateway/run.py::_prepend_model_badge` (~L1495), called from the resend path | Resend runs its body through the idempotent `_prepend_model_badge`. Test: `tests/gateway/test_resend_badge_prepend.py`. |
| 3 | Overflow-split multi-chunk messages dropped the badge on the first chunk. | `gateway/stream_consumer.py::_send_new_chunk` (~L939) | First chunk is badged; now via the `_adapter_send` choke point (previously an inline per-site guard). |
| 4 | **Fresh-final / draft-frame** — the completed Telegram reply is delivered by `_try_fresh_final` (a NEW message + delete of the stale streaming preview) and, under native draft streaming, by `_send_draft_frame`. Both are direct adapter calls that historically had no guard of their own. | `gateway/stream_consumer.py::_try_fresh_final` (~L1416), `_send_draft_frame` (~L1201) | `_try_fresh_final` routes through the `_adapter_send` choke point. `_send_draft_frame` uses `adapter.send_draft` (not `_adapter_send`), so it applies the **same first-visible-message guard** via `_apply_model_badge` directly (~L1215) — it no longer relies only on `_send_or_edit`'s prepend, so a direct caller can't drop the badge. Direct-call RED→GREEN tests in `tests/gateway/test_stream_consumer_fresh_final_badge.py`; via-`_send_or_edit` coverage in `test_stream_consumer_badge_send_paths.py`. |
| 5 | **Segment-tail flush** dropped the badge. After an edit failure, a tool boundary flushes un-sent tail content built straight from `self._accumulated`, bypassing `_send_or_edit`'s prepend entirely. When this was the first visible message of the turn, it went out un-badged. | `gateway/stream_consumer.py::_flush_segment_tail_on_edit_failure` (~L1241) | Routed through `_adapter_send` (the choke point badges it when it is the turn's first visible message). RED→GREEN test in `tests/gateway/test_stream_consumer_badge_send_paths.py`. |

Also routed through the choke point for the same guarantee: `_send_fallback_final`
(~L1013), `_send_commentary` (~L1294), and the `_send_or_edit` first-send site.

### INFRA hazard: `hermes-up` has wiped the badge commits twice

`hermes-up` rebuilds `birk-local` fresh from `origin/main` and re-merges the
feature branches listed in `~/.hermes/hermes-agent-features.txt`. If the badge
work is not on a listed branch, a rebuild deletes it. Mitigations (keep both
true): `feat/tg-badge` **must** stay listed in `hermes-agent-features.txt`, and
`gateway-branch-guard.sh` (systemd `ExecStartPre`) enforces the birk-local
checkout. All badge work — including this fix and its docs — is committed on
`feat/tg-badge` so it survives the rebuild.

### Relevant tests

```
venv/bin/python -m pytest \
  tests/gateway/test_model_badge.py \
  tests/gateway/test_stream_consumer_model_badge.py \
  tests/gateway/test_resend_badge_prepend.py \
  tests/gateway/test_stream_consumer_badge_send_paths.py \
  tests/gateway/test_stream_consumer_fresh_final_badge.py -q
```
