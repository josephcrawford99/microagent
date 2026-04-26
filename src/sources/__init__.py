# Source plugins live as flat modules under this package; each exports
# its concrete `Source` subclass via `Plugin = ClassName`. Interfaces
# (sources that can also send) live one level deeper at `sources.interfaces.*`.
# Loader: `lib.plugins.load_input(kind, name)` — no registry walk.
