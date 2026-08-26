"""Counters, in one place, on a registry per app.

A registry per app rather than the global default, because the test suite builds
several apps in one process and the global one raises on a duplicate
registration. It also means an app instance can be thrown away cleanly.

The funnel counters are the point of the service. Everything else is
operational, and the one that quietly kills the whole thing is the token expiry
gauge.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        reg = self.registry

        # --- The funnel ----------------------------------------------------
        self.comments_matched = Counter(
            "reelsmith_comments_matched_total",
            "Comments matching a post keyword, claimed for a private reply",
            registry=reg,
        )
        self.private_replies = Counter(
            "reelsmith_private_replies_total", "Private replies sent to comments", registry=reg
        )
        self.inbound_messages = Counter(
            "reelsmith_inbound_messages_total", "Messages received from users", registry=reg
        )
        self.follow_checks = Counter(
            "reelsmith_follow_checks_total", "Profile reads asking whether a user follows",
            registry=reg,
        )
        self.follows_confirmed = Counter(
            "reelsmith_follows_confirmed_total", "Follow checks that came back true", registry=reg
        )
        self.nudges_sent = Counter(
            "reelsmith_nudges_sent_total", "Reminders asking for the follow", registry=reg
        )
        self.links_sent = Counter(
            "reelsmith_links_sent_total", "Links delivered, the conversion", registry=reg
        )

        # --- Operational ---------------------------------------------------
        self.webhook_signature_failures = Counter(
            "reelsmith_webhook_signature_failures_total",
            "Webhook POSTs rejected before parsing",
            registry=reg,
        )
        self.graph_errors = Counter(
            "reelsmith_graph_errors_total", "Errors returned by the Meta API", registry=reg
        )
        self.window_lapsed = Counter(
            "reelsmith_window_lapsed_total",
            "Sends skipped because the 24 hour window had closed",
            registry=reg,
        )
        self.poll_cycles = Counter(
            "reelsmith_poll_cycles_total", "Completed comment poll sweeps", registry=reg
        )
        self.poller_last_success = Gauge(
            "reelsmith_poller_last_success_timestamp",
            "Unix time of the last comment sweep that finished",
            registry=reg,
        )
        self.token_days_left = Gauge(
            "reelsmith_token_days_left",
            "Days before an account token expires, the thing that silently ends everything",
            # Deliberately still `ig_user_id`, where everything else is now
            # `account_id`. A Prometheus label is part of a series' identity and
            # the alert rules that read it are in the homelab repo, so renaming
            # it here would break them from a change with no behaviour in it.
            # It moves in a homelab PR or not at all. F10, F7.
            ["ig_user_id"],
            registry=reg,
        )

        # --- The scheduled queue -------------------------------------------
        self.posts_published = Counter(
            "reelsmith_posts_published_total", "Reels published from the queue", registry=reg
        )
        self.publish_failures = Counter(
            "reelsmith_publish_failures_total", "Publish attempts that did not complete",
            registry=reg,
        )
        # The one to alert on. A slot firing into an empty queue is the shape of
        # "the account went quiet three days ago and nobody noticed", and it is
        # invisible from every other counter here.
        self.slots_starved = Counter(
            "reelsmith_slots_starved_total",
            "Slots that came due with nothing approved to publish",
            registry=reg,
        )
        self.scheduler_last_success = Gauge(
            "reelsmith_scheduler_last_success_timestamp",
            "Unix time of the last scheduler tick that finished",
            registry=reg,
        )
        self.queue_depth = Gauge(
            "reelsmith_queue_depth", "Posts in the queue, by state", ["state"], registry=reg
        )
        self.insights_fetched = Counter(
            "reelsmith_insights_fetched_total",
            "Per-media insight readings stored",
            registry=reg,
        )
        self.insights_last_success = Gauge(
            "reelsmith_insights_last_success_timestamp",
            "Unix time of the last insights sweep that finished",
            registry=reg,
        )
        self.backup_last_success = Gauge(
            "reelsmith_backup_last_success_timestamp",
            "Unix time of the last state backup that finished",
            registry=reg,
        )

    def export(self) -> bytes:
        return generate_latest(self.registry)
