"""create_topic tool -- agent-callable creation of Telegram DM topics.

Deliberately narrow: unlike ``send_message`` (which is intentionally NOT
agent-callable — see the note at the bottom of tools/send_message_tool.py,
the agent must not fire arbitrary cross-platform messages/reactions on its
own), this tool exposes ONLY the "create a new DM topic" capability. It lets
the agent split a conversation into a fresh thread AFTER the user has
explicitly confirmed the split — nothing else. No message sending, no
reactions, no cross-platform reach.

Reuses the transport logic in ``send_message_tool._handle_create_topic`` so
there is a single implementation of the createForumTopic path.

CONTEXT HANDOFF (the delegate_task analogy): a freshly created topic is an
EMPTY session — the agent answering there knows nothing about the main
conversation it was split from. Exactly like ``delegate_task(context=...)``
briefs an isolated subagent, ``create_topic(context=...)`` seeds a handoff
brief into the new topic's session so the first reply there already has the
carried-over context. Implemented via ``gateway.mirror.mirror_to_session``
(role="user", the same primitive cron ``attach_to_session`` uses to avoid
"what is this?" amnesia).

Requires a live Telegram adapter in the running gateway (same constraint as
reactions) — not available from cron/standalone contexts.
"""

import json

from tools.registry import registry


CREATE_TOPIC_SCHEMA = {
    "name": "create_topic",
    "description": (
        "Create a new Telegram DM topic (a forum thread inside a private chat) "
        "so a new conversation context gets its own isolated thread.\n\n"
        "USE ONLY on Telegram, ONLY when the user has DM-topics mode enabled, "
        "and ONLY after the user has EXPLICITLY confirmed the split. Propose the "
        "new topic first (name it, say why), wait for a clear yes, THEN call this. "
        "NEVER create topics unprompted or speculatively — an unwanted topic is "
        "clutter the user has to clean up.\n\n"
        "CONTEXT HANDOFF: a new topic is a FRESH, EMPTY session — the agent "
        "answering there will know nothing about this conversation. If the new "
        "thread needs to carry over anything from here (decisions, facts, the "
        "task so far, file paths, constraints), pass a self-contained `context` "
        "brief — exactly like briefing a subagent with delegate_task(context=...). "
        "It is seeded as the first turn in the new topic so the next reply there "
        "has what it needs. Omit `context` only for a genuinely clean-slate topic "
        "(e.g. a brand-new unrelated subject).\n\n"
        "This tool cannot send messages, react, or reach other platforms — it "
        "only creates the thread (optionally seeding the context brief) and "
        "returns its target id."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": (
                    "Telegram DM chat to create the topic in, as 'telegram:chat_id' "
                    "(no thread_id). Omit to use the Telegram home channel."
                ),
            },
            "topic_name": {
                "type": "string",
                "description": "Name of the new topic (e.g. 'Steuer 2025'). Required.",
            },
            "context": {
                "type": "string",
                "description": (
                    "Optional self-contained handoff brief seeded as the first turn "
                    "of the new topic's session, so the agent answering there has "
                    "the carried-over context (decisions, facts, task state, paths, "
                    "constraints). Same idea as delegate_task's `context`. Omit for a "
                    "clean-slate topic on a genuinely new subject."
                ),
            },
        },
        "required": ["topic_name"],
    },
}


def _seed_context(chat_id: str, thread_id: str, context: str) -> bool:
    """Seed a handoff brief into the freshly created topic's session.

    A topic created via the Bot API (createForumTopic) does NOT yet have a
    gateway session — that is normally minted only when the first inbound
    message for the thread arrives. So mirror_to_session alone finds no target
    and silently no-ops (the `context_seeded: false` bug). We must first create
    the thread-keyed session explicitly, exactly as the cron scheduler does in
    `_seed_cron_thread_session` (via session_store.get_or_create_session on a
    thread-typed SessionSource), THEN mirror the brief into it.

    Uses mirror_to_session with role="user" (a user-role mirror collapses
    safely via repair_message_sequence on every provider; an assistant-role
    mirror would risk assistant→assistant alternation breakage — see mirror.py).
    Best-effort: never raise; a failed seed must not fail topic creation.
    """
    try:
        # 1. Reach the live Telegram adapter + its session store.
        from gateway.config import Platform
        from gateway.session import SessionSource

        runner = None
        try:
            from gateway.run import _gateway_runner_ref
            runner = _gateway_runner_ref()
        except Exception:
            runner = None
        adapter = (
            runner.adapters.get(Platform.TELEGRAM)
            if runner is not None and getattr(runner, "adapters", None)
            else None
        )
        session_store = getattr(adapter, "_session_store", None) if adapter else None

        # 2. Ensure the thread-keyed session row exists so the mirror has a
        #    target AND the user's later in-thread reply resolves to the SAME
        #    session (build_session_key keys threads as participant-shared, so
        #    no user_id is needed — matches the cron seed).
        if session_store is not None:
            dest_source = SessionSource(
                platform=Platform.TELEGRAM,
                chat_id=str(chat_id),
                chat_type="thread",
                thread_id=str(thread_id),
            )
            session_store.get_or_create_session(dest_source)

        # 3. Mirror the brief into the (now guaranteed) session.
        from gateway.mirror import mirror_to_session
        brief = (
            "[Kontext-Übergabe aus dem vorherigen Thread — dieses Topic wurde "
            "aus einem laufenden Gespräch abgezweigt. Fortsetzung mit diesem "
            "Kontext:]\n\n" + context.strip()
        )
        return bool(mirror_to_session(
            "telegram",
            str(chat_id),
            brief,
            source_label="create_topic",
            thread_id=str(thread_id),
            role="user",
        ))
    except Exception:
        return False


def create_topic_tool(args, **kw):
    """Create the topic, then optionally seed a context handoff brief."""
    from tools.send_message_tool import _handle_create_topic
    a = dict(args or {})
    if not a.get("target"):
        a["target"] = "telegram"
    context = (a.pop("context", None) or "").strip()

    raw = _handle_create_topic(a)
    try:
        result = json.loads(raw)
    except Exception:
        return raw

    # Only seed if the topic was created and a context brief was supplied.
    if context and isinstance(result, dict) and result.get("success"):
        thread_id = result.get("thread_id")
        target = result.get("target", "")
        # target is "telegram:<chat_id>:<thread_id>"
        chat_id = None
        parts = target.split(":")
        if len(parts) >= 2:
            chat_id = parts[1]
        if chat_id and thread_id:
            seeded = _seed_context(chat_id, thread_id, context)
            result["context_seeded"] = seeded
        else:
            result["context_seeded"] = False
        return json.dumps(result)

    return raw


def check_create_topic_requirements() -> bool:
    """Available whenever the send_message transport module imports cleanly.

    The real gate (live Telegram adapter present) is enforced at call time by
    ``_handle_create_topic``; here we only confirm the code path is loadable so
    the tool is advertised in gateway contexts.
    """
    try:
        from tools.send_message_tool import _handle_create_topic  # noqa: F401
        return True
    except Exception:
        return False


registry.register(
    name="create_topic",
    toolset="messaging",
    schema=CREATE_TOPIC_SCHEMA,
    handler=create_topic_tool,
    check_fn=check_create_topic_requirements,
    emoji="🧵",
)
