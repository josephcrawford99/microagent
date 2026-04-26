# Dashboard fields live as flat `dashboard_*` attributes on `lib.settings.RootConfig`
# now — there is no separate DashboardSettings model. This package only exports
# the server itself, lazy-loaded to keep `dashboard.server`'s deps off the
# import-time graph.
def __getattr__(name: str):
    if name == "DashboardServer":
        from .server import DashboardServer
        return DashboardServer
    raise AttributeError(f"module 'dashboard' has no attribute {name!r}")


__all__ = ["DashboardServer"]
