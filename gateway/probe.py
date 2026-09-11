"""Asking a platform for every metric it has, and forgetting the ones it refuses.

Every platform here fails a whole metrics request when one name in it is
retired, misspelt or not offered for this kind of post, and none of them says
which. A Page answers "(#100) The value must be a valid insights metric",
Instagram answers "An unknown error has occurred", and Google answers a 400
about the query. So a list written from the documentation is a list that stops
returning anything the day one entry retires, which is what happened to every
Facebook read on 2026-09-11.

A read asks for everything. On a refusal it adds the names back one at a time,
in the order they are listed, keeping each one the endpoint still answers with.
The ones that broke the request are remembered for the life of the process,
named in one warning, and left out of every read after. A restart asks again,
which is how a metric a platform starts offering later arrives without a deploy.

**One at a time rather than each alone**, because a name can be fine by itself
and refused beside another. Meta's generic `(#1) An unknown error has occurred`
is documented as exactly that on the Instagram edge. Adding names back finds
both cases with one request per name, and **the order is the priority**: of two
names that will not be served together the earlier one is kept, so a list puts
the metrics that fill a column first.

**A refusal of every metric is not a refusal of any one.** When not even the
first name reads on its own, the likelier story is an endpoint having a bad
minute or a post with no numbers yet, and remembering that would switch the
read off until the next rollout. Nothing is remembered and the caller sees the
refusal.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

_EVERY: list[Refusals] = []


class Refusals:
    """The metric names one endpoint has refused on this process."""

    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self.names: set[str] = set()
        _EVERY.append(self)

    def wanted(self, metrics: Sequence[str]) -> list[str]:
        return [metric for metric in metrics if metric not in self.names]

    def clear(self) -> None:
        self.names.clear()

    async def read(
        self,
        metrics: Sequence[str],
        fetch: Callable[[list[str]], Awaitable[T]],
        refused: Callable[[T], bool],
    ) -> T | None:
        """The outcome of asking for every metric not already refused.

        None when there is nothing left to ask for. Otherwise the last outcome
        `fetch` produced, which is itself a refusal only when no smaller set
        could be read either.
        """
        wanted = self.wanted(metrics)
        if not wanted:
            return None
        outcome = await fetch(wanted)
        if not refused(outcome) or len(wanted) == 1:
            return outcome

        kept: list[str] = []
        bad: list[str] = []
        last: T | None = None
        for metric in wanted:
            attempt = await fetch([*kept, metric])
            if refused(attempt):
                bad.append(metric)
            else:
                kept.append(metric)
                last = attempt
        if not kept:
            return outcome
        if bad:
            self.names.update(bad)
            log.warning(
                "%s refused the metric(s) %s; reading without them",
                self.endpoint, ", ".join(bad),
            )
        # The last accepted attempt asked for exactly `kept`, so it is already
        # the reading and there is nothing to fetch again.
        return last


def forget_all() -> None:
    """For tests, which share one process and must not share what was refused."""
    for refusals in _EVERY:
        refusals.clear()
