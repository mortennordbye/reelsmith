# Privacy policy

**The policy lives at <https://gate.nordbye.it/privacy>**, and that page is the
only copy.

It moved there on 2026-08-27 because TikTok will not accept a privacy policy URL
that is not on a domain verified by DNS record, which `github.com` can never be.
The same verification covers the media TikTok pulls from that host, so one DNS
record serves both.

The page is `gateway/templates/privacy.html`. Keeping the markdown here as well
would mean two versions of a privacy policy drifting apart, which is worse than
a file that points at the real one. `gateway/pages.py` explains why it is served
from a router that mounts whether or not the admin panel is enabled.

The terms of service moved the same way, to <https://gate.nordbye.it/terms>.
