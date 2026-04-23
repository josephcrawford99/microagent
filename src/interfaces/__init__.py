from lib.interface import Interface
from lib.registry import discover

import sys

INTERFACES: dict[str, type[Interface]] = discover(sys.modules[__name__], Interface)
