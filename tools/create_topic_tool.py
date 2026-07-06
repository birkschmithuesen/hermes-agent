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

Requires a live Telegram adapter in the running gateway (same constraint as
reactions) — not available from cron/standalone contexts.
"""

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
        "This tool cannot send messages, react, or reach other platforms — it "
        "only creates the thread and returns its target id."
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
        },
        "required": ["topic_name"],
    },
}


def create_topic_tool(args, **kw):
    """Delegate to the shared create-topic implementation in send_message_tool."""
    from tools.send_message_tool import _handle_create_topic
    # Default target to the Telegram home channel when omitted.
    a = dict(args or {})
    if not a.get("target"):
        a["target"] = "telegram"
    return _handle_create_topic(a)


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
