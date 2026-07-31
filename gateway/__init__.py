"""The comment to DM gateway.

A Reel says "comment SEND and I will DM you the link". This service is what
makes that true: it watches the comments on published posts, sends each
commenter the one private reply Meta allows, asks them to follow, and sends the
link once they have.

It runs in the homelab cluster rather than on the laptop, and it deliberately
holds nothing that could reproduce the voice. The pipeline can run with this
service down: covers fall back to a video frame, post registration is
best effort, and a comment missed while it was offline is still inside the
seven day reply window when it comes back.

Layout, in the order a request moves through it:

    webhook.py       Meta's inbound events, signature checked before parsing
    conversations.py the DM state machine, and the three API invariants
    poller.py        the comment poller and the token refresher
    graph.py         every call this makes to Meta, and nothing else
    db.py            SQLite, one file, no ORM
    api.py           what the pipeline on the Mac talks to
"""
