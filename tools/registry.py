"""
The names a server can elect into.

A tool is only reachable from configuration once it appears here, which keeps
the set of things a config file can switch on a closed, readable list rather
than whatever happens to be importable.

To add one: define the class, import it here, and add it to `TOOLS` under the
name the config file will use.
"""

from __future__ import annotations

from collections.abc import Mapping

from tools.base import Tool

TOOLS: Mapping[str, type[Tool]] = {}


def lookup(name: str) -> type[Tool] | None:
    """The tool class registered under a name, or None if nothing answers to it."""
    return TOOLS.get(name)
