# Interface plugins live as flat modules under this package; each module
# exports its concrete `Interface` subclass via `Plugin = ClassName`. The
# loader is `lib.plugins.load_input("interfaces", name)` — no registry walk.
