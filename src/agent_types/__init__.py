from lib.agent import AgentType
from lib.registry import discover

import sys

AGENT_TYPES: dict[str, type[AgentType]] = discover(sys.modules[__name__], AgentType)
