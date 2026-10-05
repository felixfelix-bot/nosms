"""Offline tests for the OpenBao secret store read path (``app.jmp_secrets``).

The ``OpenBaoSecretStore`` reads JMP credentials by shelling out to the
``fleet_secret.py`` helper. Everything here monkeypatches ``subprocess.run`` and
``os.path.isfile`` so nothing is really executed — the point is to exercise
every branch of ``read`` (absent helper, unreachable store, non-zero exit, bad
JSON, non-dict/empty payload, happy path) and the ``OperatorAllowList.from_env``
parsing, which the live tests never reach because they use ``MemorySecretStore``.
"""
from __future__ import annotations

import subprocess

import pytest

from app.jmp_secrets import (
    DEFAULT_FLEET_SECRET,
    OpenBaoSecretStore,
    OperatorAllowList,
    SecretRead,
)


class _Completed:
    """A stand-in for ``subprocess.CompletedProcess`` (the bits ``read`` reads)."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_init_defaults_to_the_standard_helper_path(monkeypatch):
    monkeypatch.delenv("NOSMS_FLEET_SECRET", raising=False)
    import os

    store = OpenBaoSecretStore()
    assert store.fleet_secret_path == os.path.expanduser(DEFAULT_FLEET_SECRET)
    assert store.timeout == 10.0
    assert store.env is None


def test_init_honours_an_explicit_path_and_env(monkeypatch):
    monkeypatch.setenv("NOSMS_FLEET_SECRET", "/unused/env/path")
    store = OpenBaoSecretStore(fleet_secret_path="/explicit/helper.py",
                               timeout=3.0, env={"A": "1"})
    assert store.fleet_secret_path == "/explicit/helper.py"
    assert store.timeout == 3.0
    assert store.env == {"A": "1"}


def test_init_falls_back_to_env_var(monkeypatch):
    monkeypatch.setenv("NOSMS_FLEET_SECRET", "/from/env/helper.py")
    store = OpenBaoSecretStore()
    assert store.fleet_secret_path == "/from/env/helper.py"


def test_read_when_helper_is_not_a_file_fails_closed(monkeypatch):
    monkeypatch.setattr("app.jmp_secrets.os.path.isfile", lambda p: False)
    store = OpenBaoSecretStore(fleet_secret_path="/nope/helper.py")
    read = store.read("jmp/account")
    assert isinstance(read, SecretRead)
    assert read.present is False
    assert read.store == "openbao"
    assert read.name == "jmp/account"
    assert "fleet_secret helper not found" in read.detail
    assert "/nope/helper.py" in read.detail


def test_read_when_run_raises_oserror_is_unreachable(monkeypatch):
    monkeypatch.setattr("app.jmp_secrets.os.path.isfile", lambda p: True)

    def boom(*a, **k):
        raise OSError("no such file")

    monkeypatch.setattr("app.jmp_secrets.subprocess.run", boom)
    read = OpenBaoSecretStore(fleet_secret_path="/helper.py").read("jmp/account")
    assert read.present is False
    assert "OSError" in read.detail
    assert "could not be reached" in read.detail


def test_read_when_run_raises_subprocess_error_is_unreachable(monkeypatch):
    monkeypatch.setattr("app.jmp_secrets.os.path.isfile", lambda p: True)

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("helper", 10.0)

    monkeypatch.setattr("app.jmp_secrets.subprocess.run", boom)
    read = OpenBaoSecretStore(fleet_secret_path="/helper.py").read("jmp/account")
    assert read.present is False
    assert "TimeoutExpired" in read.detail
    assert "could not be reached" in read.detail


def test_read_nonzero_exit_reports_the_last_stderr_line(monkeypatch):
    monkeypatch.setattr("app.jmp_secrets.os.path.isfile", lambda p: True)

    def run(*a, **k):
        return _Completed(returncode=1, stderr="line one\nline two\nthe real error\n")

    monkeypatch.setattr("app.jmp_secrets.subprocess.run", run)
    read = OpenBaoSecretStore(fleet_secret_path="/helper.py").read("jmp/account")
    assert read.present is False
    assert "rc=1" in read.detail
    assert read.detail.endswith("the real error")


def test_read_nonzero_exit_without_stderr_names_that(monkeypatch):
    monkeypatch.setattr("app.jmp_secrets.os.path.isfile", lambda p: True)
    monkeypatch.setattr("app.jmp_secrets.subprocess.run",
                        lambda *a, **k: _Completed(returncode=2, stderr=""))
    read = OpenBaoSecretStore(fleet_secret_path="/helper.py").read("jmp/account")
    assert read.present is False
    assert "rc=2" in read.detail
    assert "no stderr" in read.detail


def test_read_stdout_that_is_not_json_is_refused(monkeypatch):
    monkeypatch.setattr("app.jmp_secrets.os.path.isfile", lambda p: True)
    monkeypatch.setattr("app.jmp_secrets.subprocess.run",
                        lambda *a, **k: _Completed(returncode=0, stdout="not json at all"))
    read = OpenBaoSecretStore(fleet_secret_path="/helper.py").read("jmp/account")
    assert read.present is False
    assert "not JSON" in read.detail


def test_read_empty_stdout_is_refused(monkeypatch):
    monkeypatch.setattr("app.jmp_secrets.os.path.isfile", lambda p: True)
    monkeypatch.setattr("app.jmp_secrets.subprocess.run",
                        lambda *a, **k: _Completed(returncode=0, stdout=""))
    read = OpenBaoSecretStore(fleet_secret_path="/helper.py").read("jmp/account")
    assert read.present is False
    assert "secret is empty" in read.detail


def test_read_non_dict_payload_is_refused(monkeypatch):
    monkeypatch.setattr("app.jmp_secrets.os.path.isfile", lambda p: True)
    monkeypatch.setattr("app.jmp_secrets.subprocess.run",
                        lambda *a, **k: _Completed(returncode=0, stdout='["a", "list"]'))
    read = OpenBaoSecretStore(fleet_secret_path="/helper.py").read("jmp/account")
    assert read.present is False
    assert "secret is empty" in read.detail


def test_read_empty_dict_payload_is_refused(monkeypatch):
    monkeypatch.setattr("app.jmp_secrets.os.path.isfile", lambda p: True)
    monkeypatch.setattr("app.jmp_secrets.subprocess.run",
                        lambda *a, **k: _Completed(returncode=0, stdout="{}"))
    read = OpenBaoSecretStore(fleet_secret_path="/helper.py").read("jmp/account")
    assert read.present is False
    assert "secret is empty" in read.detail


def test_read_happy_path_returns_the_values(monkeypatch):
    monkeypatch.setattr("app.jmp_secrets.os.path.isfile", lambda p: True)
    payload = '{"jid": "hermes@jabber.fr", "secret": "s3cret"}'
    captured = {}

    def run(args, **k):
        captured["args"] = args
        return _Completed(returncode=0, stdout=payload)

    monkeypatch.setattr("app.jmp_secrets.subprocess.run", run)
    read = OpenBaoSecretStore(fleet_secret_path="/helper.py").read("jmp/account")
    assert read.present is True
    assert read.store == "openbao"
    assert read.name == "jmp/account"
    assert read.values == {"jid": "hermes@jabber.fr", "secret": "s3cret"}
    # the helper is invoked with the right argument vector
    args = captured["args"]
    assert args[-2:] == ["get", "jmp/account"]


def test_read_uses_the_python_interpreter(monkeypatch):
    import sys

    monkeypatch.setattr("app.jmp_secrets.os.path.isfile", lambda p: True)
    captured = {}

    def run(args, **k):
        captured["args"] = args
        return _Completed(returncode=0, stdout='{"jid": "x"}')

    monkeypatch.setattr("app.jmp_secrets.subprocess.run", run)
    OpenBaoSecretStore(fleet_secret_path="/helper.py").read("jmp/account")
    assert captured["args"][0] == (sys.executable or "python3")


def test_read_env_is_copied_when_none_supplied(monkeypatch):
    monkeypatch.setattr("app.jmp_secrets.os.path.isfile", lambda p: True)
    captured = {}

    def run(args, **k):
        captured["env"] = k.get("env")
        return _Completed(returncode=0, stdout='{"jid": "x"}')

    monkeypatch.setattr("app.jmp_secrets.subprocess.run", run)
    OpenBaoSecretStore(fleet_secret_path="/helper.py").read("jmp/account")
    assert captured["env"] is not None
    assert "PATH" in captured["env"]


def test_read_env_is_passed_through_when_supplied(monkeypatch):
    monkeypatch.setattr("app.jmp_secrets.os.path.isfile", lambda p: True)
    captured = {}

    def run(args, **k):
        captured["env"] = k.get("env")
        return _Completed(returncode=0, stdout='{"jid": "x"}')

    monkeypatch.setattr("app.jmp_secrets.subprocess.run", run)
    OpenBaoSecretStore(fleet_secret_path="/helper.py", env={"CUSTOM": "v"}).read("jmp/account")
    assert captured["env"] == {"CUSTOM": "v"}


# --- OperatorAllowList.from_env (the lines the memory-store tests never touch) ---

def test_allow_list_from_env_defaults_empty():
    assert OperatorAllowList.from_env(env={}).pubkeys == frozenset()


def test_allow_list_from_env_parses_comma_and_semicolon_and_trims():
    allow = OperatorAllowList.from_env(
        env={"NOSMS_JMP_ALLOWLIST": "  AABBCC ; ddeeff,FfEeDd, "})
    assert allow.pubkeys == frozenset({"aabbcc", "ddeeff", "ffeedd"})


def test_allow_list_case_insensitive_membership():
    allow = OperatorAllowList(frozenset({"aabbcc"}))
    assert allow.allows("AABBCC")
    assert allow.allows("  aabbcc  ")
    assert not allow.allows("zzzz")
    assert not allow.allows(None)
    assert not allow.allows("")
