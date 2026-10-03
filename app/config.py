"""Runtime configuration. Every knob is an environment variable; no secrets here.

`NOSMS_TRANSPORT` names the rail the service *reports* it is configured with. The
health endpoint must never probe the rail to answer a cheap liveness question, so
this value is configuration, not a live call.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace

SERVICE_NAME = "nosms"
DEFAULT_VERSION = "0.1.0-m1a"
DEFAULT_TRANSPORT = "email_gateway"


@dataclass(frozen=True)
class Config:
    service: str = SERVICE_NAME
    version: str = DEFAULT_VERSION
    commit: str | None = None
    transport: str = DEFAULT_TRANSPORT
    env: str = "dev"

    @classmethod
    def from_env(cls, environ: dict | None = None) -> "Config":
        env = os.environ if environ is None else environ
        return cls(
            service=env.get("NOSMS_SERVICE", SERVICE_NAME),
            version=env.get("NOSMS_VERSION", DEFAULT_VERSION),
            commit=(env.get("NOSMS_COMMIT") or "").strip() or None,
            transport=env.get("NOSMS_TRANSPORT", DEFAULT_TRANSPORT),
            env=env.get("NOSMS_ENV", "dev"),
        )

    def with_transport(self, name: str) -> "Config":
        return replace(self, transport=name)
