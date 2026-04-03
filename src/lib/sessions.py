import json
import os
import time


def _sessions_path(data_dir):
    return os.path.join(data_dir, "sessions", "sessions.json")


def _load_sessions(data_dir):
    path = _sessions_path(data_dir)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def _save_sessions(data_dir, sessions):
    path = _sessions_path(data_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(sessions, f, indent=2)


def get_session_id(data_dir, thread_id, ttl=None):
    """Get the claude session ID for a thread, or None if expired/missing.

    ttl: max age in seconds, or "daily" to expire at midnight.
    """
    sessions = _load_sessions(data_dir)
    entry = sessions.get(thread_id)
    if not entry:
        return None

    created = entry.get("created", 0)
    now = time.time()

    if ttl == "daily":
        # Expire if created before today's midnight
        today_midnight = now - (now % 86400)  # UTC midnight
        if created < today_midnight:
            return None
    elif isinstance(ttl, (int, float)) and ttl > 0:
        if now - created > ttl:
            return None

    return entry.get("session_id")


def save_session_id(data_dir, thread_id, session_id):
    """Store a claude session ID for a thread with a timestamp."""
    sessions = _load_sessions(data_dir)
    sessions[thread_id] = {
        "session_id": session_id,
        "created": time.time(),
    }
    _save_sessions(data_dir, sessions)
