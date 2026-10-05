"""Reading the JMP account's access material from the fleet secret store.

Store: OpenBao, read through ``fleet_secret.py`` (ADR-014 in-memory delivery,
ADR-020 AppRole). There is deliberately **no write path here**: this node's role
has `read` on `secret/data/fleet/*` and `secret/data/nodes/cobrador/*` and no
`create`/`update` anywhere, so a service that tried to store a credential would
fail at runtime instead of at review. Storing the JMP material into OpenBao is an
operator/admin action; the service only ever reads.

Fail-closed, never a fallback
-----------------------------
KeePass is RETIRED and a plaintext ``.env`` is worse. If the credential is not in
OpenBao this module reports *which* store it asked, *which* name, and that the
value was absent — a diagnosable refusal. It never substitutes a local file, and
no caller may turn a failed read into an empty secret.

Never logged
------------
The values returned here are the highest-risk data the service handles. Every
read is paired with :func:`redaction_list`, which feeds the driver's transcript
redactor, and nothing in this module prints a value (errors carry the store name
and the secret *name*, never the content).
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Protocol

#: logical names inside OpenBao (fleet_secret maps `name` -> secret/data/fleet/name).
ACCOUNT_SECRET = "jmp/account"
NUMBER_SECRET = "jmp/number"
FUNDING_SECRET = "jmp/funding"

DEFAULT_FLEET_SECRET = "~/.hermes/scripts/fleet_secret.py"

#: The field names each logical secret is expected to carry.
ACCOUNT_FIELDS = ("jid", "secret")
NUMBER_FIELDS = ("number", "number_state", "reserved_at")
FUNDING_FIELDS = ("btc_address", "amount_btc", "amount_usd_min", "captured_at")


@dataclass(frozen=True)
class SecretRead:
    """The outcome of one read — present or not, and always diagnosable."""

    store: str
    name: str
    present: bool
    values: dict = field(default_factory=dict)
    detail: str = ""

    def field(self, key: str) -> str | None:
        value = self.values.get(key)
        return None if value is None else str(value)

    def missing_fields(self, expected) -> list[str]:
        return [k for k in expected if not self.field(k)]

    def as_public_dict(self) -> dict:
        """The read, safe to log: never contains a value."""
        return {"store": self.store, "name": self.name, "present": self.present,
                "fields": sorted(self.values), "detail": self.detail}


class SecretStore(Protocol):
    """Anything that can answer one read by logical name."""

    name: str

    def read(self, name: str) -> SecretRead:  # pragma: no cover - protocol
        ...


class OpenBaoSecretStore:
    """Reads through ``fleet_secret.py get <name>`` (JSON on stdout)."""

    name = "openbao"

    def __init__(self, fleet_secret_path: str | None = None, timeout: float = 10.0,
                 env: dict | None = None):
        self.fleet_secret_path = os.path.expanduser(
            fleet_secret_path or os.environ.get("NOSMS_FLEET_SECRET", DEFAULT_FLEET_SECRET))
        self.timeout = timeout
        self.env = env

    def read(self, name: str) -> SecretRead:
        if not os.path.isfile(self.fleet_secret_path):
            return SecretRead(self.name, name, False, {},
                              f"fleet_secret helper not found at {self.fleet_secret_path}")
        try:
            proc = subprocess.run(
                [self._python(), self.fleet_secret_path, "get", name],
                capture_output=True, text=True, timeout=self.timeout,
                env=self.env if self.env is not None else os.environ.copy())
        except (OSError, subprocess.SubprocessError) as exc:
            return SecretRead(self.name, name, False, {},
                              f"{type(exc).__name__}: the store could not be reached")
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip().splitlines()
            return SecretRead(self.name, name, False, {},
                              f"fleet_secret rc={proc.returncode}: "
                              f"{detail[-1] if detail else 'no stderr'}")
        try:
            values = json.loads(proc.stdout or "{}")
        except ValueError:
            return SecretRead(self.name, name, False, {},
                              "store returned a value that is not JSON")
        if not isinstance(values, dict) or not values:
            return SecretRead(self.name, name, False, {}, "secret is empty")
        return SecretRead(self.name, name, True, {str(k): v for k, v in values.items()})

    @staticmethod
    def _python() -> str:
        import sys
        return sys.executable or "python3"


class MemorySecretStore:
    """An in-memory store for tests: same shape, no OpenBao, no subprocess."""

    def __init__(self, values: dict[str, dict] | None = None, name: str = "openbao"):
        self.name = name
        self.values = dict(values or {})

    def read(self, name: str) -> SecretRead:
        values = self.values.get(name)
        if values is None:
            return SecretRead(self.name, name, False, {}, "secret not found: %s" % name)
        return SecretRead(self.name, name, True, dict(values))


def redaction_list(*reads: SecretRead) -> list[str]:
    """Every secret value in `reads`, for the driver's transcript redactor."""
    secrets: list[str] = []
    for read in reads:
        for value in read.values.values():
            text = str(value)
            if len(text) >= 4 and text not in secrets:
                secrets.append(text)
    return secrets


@dataclass(frozen=True)
class OperatorAllowList:
    """The pubkeys that may ask for the account credential — and nobody else.

    Empty by default: an unconfigured service refuses everyone rather than
    falling open. `allows` compares hex pubkeys case-insensitively.
    """

    pubkeys: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls, env: dict | None = None, var: str = "NOSMS_JMP_ALLOWLIST") -> "OperatorAllowList":
        env = os.environ if env is None else env
        raw = env.get(var, "")
        keys = {part.strip().lower() for part in raw.replace(";", ",").split(",") if part.strip()}
        return cls(frozenset(keys))

    def allows(self, pubkey: str | None) -> bool:
        return bool(pubkey) and str(pubkey).strip().lower() in self.pubkeys
