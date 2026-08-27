"""the EventStore conformance suite (DESIGN.md §4.1/§8).

subclass in your tests and override `make_store`; pytest collects the test
methods through the subclass. locks down: ordering, unique immutable ids,
visibility-BEFORE-windowing, message-count windows that carry intervening
non-message events, and latest-query behavior.
"""

from __future__ import annotations

import asyncio
import dataclasses

from dainframe.core.events import EventQuery, EventStore, NewEvent, VisibilityPolicy


def _msg(author_type, author, content, message_type="conversation", audience=None):
    return NewEvent(
        author_type=author_type,
        author=author,
        kind="message",
        content=content,
        message_type=message_type,
        audience=audience,
    )


def _action(author, content, audience=None):
    return NewEvent(
        author_type="agent",
        author=author,
        kind="action",
        content=content,
        audience=audience,
    )


class EventStoreContract:
    """override make_store(visibility) to test your implementation."""

    def make_store(self, visibility: VisibilityPolicy | None = None) -> EventStore:
        raise NotImplementedError

    # --- ordering & identity -------------------------------------------------

    def test_append_preserves_order_and_assigns_unique_ids(self):
        async def _run():
            store = self.make_store()
            a = await store.append(_msg("user", "user", "one"))
            b = await store.append(_msg("agent", "aria", "two"))
            c = await store.append(_action("aria", "did a thing"))
            events = await store.read(EventQuery())
            assert [e.content for e in events] == ["one", "two", "did a thing"]
            assert len({a.event_id, b.event_id, c.event_id}) == 3

        asyncio.run(_run())

    def test_events_are_immutable(self):
        async def _run():
            store = self.make_store()
            event = await store.append(_msg("user", "user", "hi"))
            try:
                event.content = "rewritten"  # type: ignore[misc]
            except dataclasses.FrozenInstanceError:
                return
            raise AssertionError("Event must be immutable")

        asyncio.run(_run())

    # --- windows -------------------------------------------------------------

    def test_message_limit_counts_messages_only_and_keeps_riders(self):
        async def _run():
            store = self.make_store()
            await store.append(_msg("user", "user", "old"))
            await store.append(_msg("agent", "aria", "m1"))
            await store.append(_action("aria", "a1"))
            await store.append(_msg("user", "user", "m2"))
            events = await store.read(EventQuery(message_limit=2))
            # the last TWO messages, with the action riding inside the window
            assert [e.content for e in events] == ["m1", "a1", "m2"]

        asyncio.run(_run())

    def test_visibility_filters_before_windowing(self):
        async def _run():
            # viewer 'candidate' must get N messages it CAN see, not a window
            # shrunk by invisible private events
            def policy(event, viewer):
                return viewer != "candidate" or event.audience != "scorers"

            store = self.make_store(visibility=policy)
            await store.append(_msg("user", "candidate", "answer 1"))
            await store.append(
                _msg("agent", "scorer", "private note", audience="scorers")
            )
            await store.append(_msg("agent", "interviewer", "follow-up"))
            visible = await store.read(EventQuery(viewer="candidate", message_limit=2))
            assert [e.content for e in visible] == ["answer 1", "follow-up"]
            unfiltered = await store.read(EventQuery(viewer="scorer", message_limit=3))
            assert [e.content for e in unfiltered] == [
                "answer 1",
                "private note",
                "follow-up",
            ]

        asyncio.run(_run())

    # --- latest --------------------------------------------------------------

    def test_latest_honors_query_filters(self):
        async def _run():
            store = self.make_store()
            await store.append(_msg("user", "user", "hello"))
            await store.append(_msg("agent", "aria", "hi!"))
            await store.append(_action("aria", "saved"))
            latest_user = await store.latest(
                EventQuery(
                    kinds=frozenset({"message"}),
                    author_types=frozenset({"user"}),
                )
            )
            assert latest_user is not None and latest_user.content == "hello"
            # a trailing action never masquerades as "the assistant replied"
            latest_message = await store.latest(
                EventQuery(kinds=frozenset({"message"}))
            )
            assert latest_message is not None and latest_message.content == "hi!"

        asyncio.run(_run())

    def test_latest_returns_none_on_empty(self):
        async def _run():
            store = self.make_store()
            assert await store.latest(EventQuery()) is None

        asyncio.run(_run())
