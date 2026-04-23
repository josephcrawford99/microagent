import sys

from lib.registry import discover
from lib.source import Source

SOURCES: dict[str, type[Source]] = discover(sys.modules[__name__], Source)
