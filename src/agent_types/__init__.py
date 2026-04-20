import importlib
import pkgutil
from lib.agent import AgentType

AGENT_TYPES: dict[str, type[AgentType]] = {}

for _finder, module_name, _ in pkgutil.iter_modules(__path__):
    module = importlib.import_module(f".{module_name}", __package__)
    for attr in vars(module).values():
        if isinstance(attr, type) and issubclass(attr, AgentType) and attr is not AgentType:
            AGENT_TYPES[attr.name] = attr
