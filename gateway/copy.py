"""What the account actually says in a DM.

This is viewer-facing text, so every rule in CLAUDE.md applies: no dashes, no
colons, no hype words, short sentences. A DM that reads like a bot template is
the fastest way to lose the person who just asked for something, and this
audience spots one instantly.

Two things here are policy rather than taste:

- **The automation is disclosed in the first message.** Meta requires it where
  law requires it (California and Germany are the ones named), and the honest
  version costs one short sentence.
- **Nothing pretends to be a person typing.** No "let me check that for you",
  no fake delay. The `human_agent` tag exists for actual humans and using it
  from automation is a policy violation, so it appears nowhere in this service.

`{link}` is the only placeholder. It is filled with a URL, which contains a
colon and often a hyphen, so `check_copy` validates the prose around it rather
than the finished string.
"""

from __future__ import annotations

# Sent as the private reply to the comment. One shot, ever, so it has to be
# worth receiving and say exactly what to do next.
#
# The follow is asked for as support rather than as a toll. "Follow to unlock"
# is the phrasing every growth-hack account uses and this audience has seen it
# a thousand times.
#
# The automation is disclosed, but last rather than first. It used to open with
# "This is an automated reply", which led with the least interesting fact in the
# message and read like a bounce notification. Phrased as speed at the end it is
# equally honest and does not cost the opening line. Meta's messaging policy
# asks for the disclosure where law requires it, naming California and Germany.
PRIVATE_REPLY = (
    "Thanks for the interest. "
    "Follow the account to support the work, then reply here and I will send the link. "
    "It is automated, so it comes straight back."
)

# They messaged but do not follow yet. Said once per inbound message, capped, so
# a follow that never arrives goes quiet instead of nagging. No guilt, no
# urgency: they owe nothing and the tone should say so.
NUDGE = (
    "Not showing as a follower yet, so I cannot send it. "
    "Tap follow and reply once more and it is yours."
)

# The payoff. Short, because the link is the point and anything wrapped around
# it is in the way.
LINK = "Here you go. {link}\n\nThanks for the follow, it genuinely helps."

# The follow arrived after a nudge, so acknowledge it rather than repeating the
# whole greeting.
LINK_LATE = "That did it. {link}\n\nThanks for the follow."

# Everything the module offers as outgoing text, so tests can sweep it.
TEMPLATES: dict[str, str] = {
    "PRIVATE_REPLY": PRIVATE_REPLY,
    "NUDGE": NUDGE,
    "LINK": LINK,
    "LINK_LATE": LINK_LATE,
}

# The same set pipeline/models.py rejects in a hook or a script. Kept as a
# literal rather than imported so the gateway image does not have to carry the
# pipeline's settings and its dependency on a checkout.
_BANNED_PUNCTUATION = frozenset(":-‐‑‒–—―−")

_BANNED_WORDS = (
    "game-changer",
    "revolutionary",
    "insane",
    "mind-blowing",
    "unlock",
    "leverage",
    "delve",
    "seamless",
    "robust",
    "elevate",
    "harness",
)


def check_copy(template: str) -> list[str]:
    """Return the reasons this template breaks the repo's text rules.

    Placeholders are removed first. A URL is not prose and the rules were never
    about it.
    """
    prose = template.replace("{link}", " ")
    problems: list[str] = []

    found = sorted({c for c in prose if c in _BANNED_PUNCTUATION})
    if found:
        problems.append(f"contains {', '.join(repr(c) for c in found)}")

    lowered = prose.lower()
    hits = [w for w in _BANNED_WORDS if w in lowered]
    if hits:
        problems.append(f"hype words {', '.join(hits)}")

    if any(ord(c) > 0x2500 for c in prose):
        problems.append("contains an emoji or symbol")

    return problems


def link_message(link: str, *, late: bool = False) -> str:
    return (LINK_LATE if late else LINK).format(link=link)
