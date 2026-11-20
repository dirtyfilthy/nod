from __future__ import annotations

from nod.inbox import InMemoryInbox


def test_post_pending_ack():
    inbox = InMemoryInbox()
    a = inbox.post("s", "first")
    b = inbox.post("s", "second")
    inbox.post("other", "elsewhere")

    assert [e.text for e in inbox.pending("s")] == ["first", "second"]
    inbox.ack("s", [a])
    assert [e.id for e in inbox.pending("s")] == [b]
    # Acking an id that isn't there is a no-op, not an error.
    inbox.ack("s", [a, "nope"])
    assert [e.id for e in inbox.pending("s")] == [b]
    assert [e.text for e in inbox.pending("other")] == ["elsewhere"]


def test_subscribers_hear_every_post():
    inbox = InMemoryInbox()
    heard: list[str] = []
    inbox.subscribe(heard.append)
    inbox.post("a", "x")
    inbox.post("b", "y")
    assert heard == ["a", "b"]


def test_post_without_waking_skips_subscribers_but_still_queues():
    inbox = InMemoryInbox()
    heard: list[str] = []
    inbox.subscribe(heard.append)
    inbox.post("s", "quiet", wake=False)
    assert heard == []
    assert [e.text for e in inbox.pending("s")] == ["quiet"]
