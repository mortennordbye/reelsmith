"""Asking a platform for every metric it has, and forgetting the ones it refuses.

Every platform here fails a whole metrics request when one name in it is
retired, misspelt or not offered for this kind of post, and none of them says
which. A Page answers "(#100) The value must be a valid insights metric",
Instagram answers "An unknown error has occurred", and Google answers a 400
about the query. So a list written from the documentation is a list that stops
returning anything the day one entry retires, which is what happened to every
Facebook read on 2026-09-11.

A read asks for everything. On a refusal it asks for each metric alone,
remembers the ones refused for the life of the process, names them in one
warning and reads again with the rest. A restart asks again, which is how a
metric a platform starts offering later arrives without a deploy.

**A refusal of every metric is not a refusal of any one.** When each metric
also fails alone, the likelier story is an endpoint having a bad minute or a
post with no numbers yet, and remembering that would switch the read off until
the next rollout. Nothing is remembered and the caller sees the refusal.
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

        bad = [metric for metric in wanted if refused(await fetch([metric]))]
        if not bad or len(bad) == len(wanted):
            return outcome
        self.names.update(bad)
        log.warning(
            "%s refused the metric(s) %s; reading without them",
            self.endpoint, ", ".join(bad),
        )
        return await fetch([metric for metric in wanted if metric not in bad])


def forget_all() -> None:
    """For tests, which share one process and must not share what was refused."""
    for refusals in _EVERY:
        refusals.clear()
