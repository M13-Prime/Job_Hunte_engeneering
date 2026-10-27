"""Optional LinkedIn channel built on agent-reach's ``mcporter`` tooling.

Disabled unless ``LINKEDIN_ENABLED=true``. See docs/LINKEDIN.md.
"""

from signal_tracker.linkedin.client import LinkedInClient, LinkedInError

__all__ = ["LinkedInClient", "LinkedInError"]
