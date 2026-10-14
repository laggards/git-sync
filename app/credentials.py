from __future__ import annotations

import os
import shlex
import tempfile
from pathlib import Path


class CredentialError(ValueError):
    """Raised when required Git SSH credentials are not configured."""


class GitCredentials:
    """Ephemeral SSH key and known-hosts files for one Git operation."""

    def __init__(self, key_path: Path, known_hosts_path: Path, temp_dir: tempfile.TemporaryDirectory):
        self.private_key_path = str(key_path)
        self.known_hosts_path = str(known_hosts_path)
        self._temp_dir = temp_dir
        self._secret_fragments = tuple(
            fragment
            for secret in (key_path.read_text(encoding="utf-8"), known_hosts_path.read_text(encoding="utf-8"))
            for fragment in (secret, *secret.splitlines())
            if fragment
        )
        hooks_path = Path(temp_dir.name) / "empty-hooks"
        hooks_path.mkdir(mode=0o700)
        hooks_path.chmod(0o700)
        self.git_environment = {
            "GIT_SSH_COMMAND": " ".join(
                [
                    "ssh",
                    "-i",
                    shlex.quote(self.private_key_path),
                    "-o IdentitiesOnly=yes",
                    "-o BatchMode=yes",
                    "-o StrictHostKeyChecking=yes",
                    "-o UserKnownHostsFile=" + shlex.quote(self.known_hosts_path),
                    "-o GlobalKnownHostsFile=/dev/null",
                ]
            ),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_SSH_VARIANT": "ssh",
            "GIT_CONFIG_COUNT": "4",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": str(hooks_path),
            "GIT_CONFIG_KEY_1": "core.fsmonitor",
            "GIT_CONFIG_VALUE_1": "false",
            "GIT_CONFIG_KEY_2": "protocol.ext.allow",
            "GIT_CONFIG_VALUE_2": "never",
            "GIT_CONFIG_KEY_3": "credential.helper",
            "GIT_CONFIG_VALUE_3": "",
        }

    @classmethod
    def from_environment(
        cls,
        environment: dict[str, str] | None = None,
        base_dir: str | Path | None = None,
    ) -> GitCredentials:
        values = os.environ if environment is None else environment
        private_key = values.get("GIT_PRIVATE_KEY", "")
        known_hosts = values.get("GIT_KNOWN_HOSTS", "")
        if not private_key.strip() or not known_hosts.strip():
            raise CredentialError("GIT_PRIVATE_KEY and GIT_KNOWN_HOSTS are required")

        temp_dir = tempfile.TemporaryDirectory(prefix="git-sync-ssh-", dir=base_dir)
        directory = Path(temp_dir.name)
        try:
            directory.chmod(0o700)
            key_path = directory / "id_key"
            hosts_path = directory / "known_hosts"
            # Compose/PowerShell can pass Windows CRLF through environment values.
            # OpenSSH may reject those bytes in PEM keys with "error in libcrypto".
            normalized_key = private_key.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
            normalized_hosts = known_hosts.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
            key_path.write_text(normalized_key, encoding="utf-8", newline="\n")
            hosts_path.write_text(normalized_hosts, encoding="utf-8", newline="\n")
            key_path.chmod(0o600)
            hosts_path.chmod(0o600)
            return cls(key_path, hosts_path, temp_dir)
        except Exception:
            temp_dir.cleanup()
            raise

    def __enter__(self) -> GitCredentials:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._temp_dir.cleanup()

    def redact(self, text: str) -> str:
        for secret in self._secret_fragments:
            text = text.replace(secret, "[REDACTED]")
        return text
