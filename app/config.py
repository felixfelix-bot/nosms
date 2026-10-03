"""Runtime configuration for the nosms service core.

Everything is env-driven: no secrets in code, no outbound probes. The
transport *name* reported by /api/health is read from config, never discovered
by calling the provider (a health check must stay cheap and side-effect free).
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, replace


def _git_sha() -> str | None:
    """Best-effort short commit sha of the deployed checkout. Never raises."""
    for var in ("NOSMS_COMMIT", "NOSMS_BUILD_SHA", "GIT_COMMIT"):
        val = os.environ.get(var)
        if val:
            return val.strip()
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:                                    # noqa: BLE001
        pass
    return None


@dataclass(frozen=True)
class Config:
    service: str = "nosms"
    version: str = "0.1.0"
    commit: str | None = None
    transport: str = "fake"
    freshness_seconds: int = 120          # NIP-98 +/- window (card: 120 s)
    replay_ttl_seconds: int = 300         # must be >= freshness window

    @classmethod
    def from_env(cls, env: dict | None = None, **overrides) -> "Config":
        e = os.environ if env is None else env
        cfg = cls(
            service=e.get("NOSMS_SERVICE", "nosms"),
            version=e.get("NOSMS_VERSION", "0.1.0"),
            commit=e.get("NOSMS_COMMIT") or e.get("NOSMS_BUILD_SHA") or _git_sha(),
            transport=e.get("NOSMS_TRANSPORT", "fake"),
            freshness_seconds=int(e.get("NOSMS_NIP98_FRESHNESS_SECONDS", "120")),
            replay_ttl_seconds=int(e.get("NOSMS_REPLAY_TTL_SECONDS", "300")),
        )
        if overrides:
            cfg = replace(cfg, **{k: v for k, v in overrides.items()
                                  if v is not None or k in cls.__dataclass_fields__})
        # the replay cache must outlive the freshness window
        if cfg.replay_ttl_seconds < cfg.freshness_seconds:
            cfg = replace(cfg, replay_ttl_seconds=cfg.freshness_seconds)
        return cfg
