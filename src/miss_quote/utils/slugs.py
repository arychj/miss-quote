"""
Reducing a name somebody chose to something safe to put in a path.

Its own module because two sides of the deployment need it and neither can
import the other: `transcript.writer` names directories with it, and `config`
matches a channel written in the file against the channel Discord reports. A
copy in each is a copy that drifts, and the two disagreeing means a room
configured under one spelling is filed under another.
"""

from __future__ import annotations

import re

SLUG_DISALLOWED = re.compile(r"[^a-z0-9_-]+")
SLUG_EDGE_CHARACTERS = "-"
SLUG_FALLBACK = "unnamed"


def slugify(name: str) -> str:
    """
    Reduce a Discord display name to something safe to use as a path segment.

    Dots and separators are dropped rather than escaped, so a name like
    `../../etc` cannot express a traversal no matter where in the string it
    appears. Runs of disallowed characters collapse to a single dash so a name
    cannot expand into a long run of separators.
    """
    slug = SLUG_DISALLOWED.sub("-", name.casefold()).strip(SLUG_EDGE_CHARACTERS)
    return slug or SLUG_FALLBACK
