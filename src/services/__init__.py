"""Services — everything that talks to an agent or takes messages from one.

Each service owns a handle directory under run/<name>/ in the home (see
lib.handle). One module per service; the loader picks up the Service
subclass it defines.
"""
