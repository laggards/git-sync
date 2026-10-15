from __future__ import annotations

import json
import ipaddress
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from pathlib import PurePosixPath
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """Raised when project configuration is invalid or unavailable."""


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    name: str
    repository: str
    branch: str
    workspace: Path
    url: str | None = None
    workspace_relative: str = ""


PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")
SCP_REPOSITORY_RE = re.compile(r"^git@([^/:\s]+):([^\s]+)$")
ALLOWED_KEYS = {"id", "name", "repository", "branch", "workspace", "url"}
_CONFIG_WRITE_LOCK = threading.RLock()
LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


def _required_string(entry: dict, key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"project field '{key}' must be a non-empty string")
    if value != value.strip() or any(ord(char) < 32 for char in value):
        raise ConfigError(f"project field '{key}' contains invalid whitespace")
    return value


def _validate_repository(repository: str) -> None:
    if any(char.isspace() or ord(char) < 32 for char in repository):
        raise ConfigError("repository address contains invalid characters")
    scp_match = SCP_REPOSITORY_RE.fullmatch(repository)
    if scp_match:
        if scp_match.group(2).startswith("-"):
            raise ConfigError("repository address is invalid")
        return

    try:
        parts = urlsplit(repository)
        port = parts.port
    except ValueError as exc:
        raise ConfigError("repository address is invalid") from exc

    if parts.scheme == "ssh":
        if not parts.hostname or parts.password or not parts.username or not parts.path.strip("/"):
            raise ConfigError("SSH repository address is invalid")
        if parts.query or parts.fragment:
            raise ConfigError("SSH repository address cannot contain query or fragment data")
        if port is not None and not 1 <= port <= 65535:
            raise ConfigError("repository port is invalid")
        return

    if parts.scheme == "https":
        if not parts.hostname or parts.username is not None or parts.password is not None:
            raise ConfigError("HTTPS repository must be public and contain no credentials")
        if parts.query or parts.fragment:
            raise ConfigError("HTTPS repository address cannot contain query or fragment data")
        if port is not None and not 1 <= port <= 65535:
            raise ConfigError("repository port is invalid")
        return
    raise ConfigError("repository must use an SSH or HTTPS Git address")


def _validate_branch(branch: str) -> None:
    if branch.startswith("-"):
        raise ConfigError("branch name is invalid")
    try:
        result = subprocess.run(
            ["git", "check-ref-format", "--branch", branch],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError("Git CLI is unavailable for branch validation") from exc
    if result.returncode != 0:
        raise ConfigError("branch name is not a valid Git branch")


def _workspace_path(value: str, root: Path) -> Path:
    if "\\" in value or not value.startswith("/workspace/"):
        raise ConfigError("workspace must be an absolute path under /workspace")
    configured = PurePosixPath(value)
    if configured.as_posix() != value or any(part in ("", ".", "..") for part in configured.parts[1:]):
        raise ConfigError("workspace path must be normalized and cannot traverse directories")
    relative_parts = configured.parts[2:]
    if not relative_parts:
        raise ConfigError("workspace cannot be the /workspace root")

    root_resolved = root.resolve(strict=False)
    lexical_candidate = root_resolved.joinpath(*relative_parts)
    candidate = root.joinpath(*relative_parts).resolve(strict=False)
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ConfigError("workspace resolves outside the mounted workspace root") from exc
    if candidate != lexical_candidate:
        raise ConfigError("workspace paths cannot pass through symbolic links")
    return candidate


def _validate_url(value: object) -> str | None:
    if value is None or value == "":
        return None
    if (
        not isinstance(value, str)
        or value != value.strip()
        or any(char.isspace() or ord(char) < 32 for char in value)
        or "\\" in value
    ):
        raise ConfigError("url must be a valid HTTP or HTTPS address")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise ConfigError("url is invalid") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError("url must use http:// or https:// and include a host")
    if parts.username is not None or parts.password is not None:
        raise ConfigError("url must not contain a username or password")
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError("url port is invalid")
    return value


def load_projects(path: str, workspace_root: str = "/workspace") -> list[Project]:
    """Load and validate the configured project allowlist."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read projects configuration: {exc.__class__.__name__}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("projects"), list):
        raise ConfigError("projects configuration must contain a projects array")

    root = Path(workspace_root)
    if not root.exists() or not root.is_dir():
        raise ConfigError("workspace root must exist and be a directory")

    projects: list[Project] = []
    ids: set[str] = set()
    for index, entry in enumerate(raw["projects"]):
        if not isinstance(entry, dict):
            raise ConfigError(f"projects[{index}] must be an object")
        unknown_keys = set(entry) - ALLOWED_KEYS
        if unknown_keys:
            raise ConfigError(f"projects[{index}] has unknown fields")

        project_id = _required_string(entry, "id")
        if not PROJECT_ID_RE.fullmatch(project_id):
            raise ConfigError("project id may contain only letters, digits, and hyphens")
        if project_id in ids:
            raise ConfigError(f"duplicate project id: {project_id}")
        ids.add(project_id)

        name = _required_string(entry, "name")
        repository = _required_string(entry, "repository")
        _validate_repository(repository)
        branch = _required_string(entry, "branch")
        _validate_branch(branch)
        workspace_value = _required_string(entry, "workspace")
        workspace = _workspace_path(workspace_value, root)
        url = _validate_url(entry.get("url"))
        workspace_relative = "/".join(PurePosixPath(workspace_value).parts[2:])
        projects.append(Project(project_id, name, repository, branch, workspace, url, workspace_relative))

    paths = sorted((project.workspace, project.id) for project in projects)
    for index, (current, current_id) in enumerate(paths):
        for other, other_id in paths[index + 1 :]:
            if current == other or current in other.parents or other in current.parents:
                raise ConfigError(f"workspace paths overlap: {current_id} and {other_id}")

    return projects


def load_allowed_ips(path: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Load the client IP allowlist from the application config file."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read access configuration: {exc.__class__.__name__}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("configuration must be a JSON object")

    values = raw.get("allowed_ips", [])
    if not isinstance(values, list):
        raise ConfigError("allowed_ips must be an array of IP addresses or CIDR networks")

    networks = []
    for index, value in enumerate(values):
        if not isinstance(value, str) or not value or value != value.strip():
            raise ConfigError(f"allowed_ips[{index}] must be an IP address or CIDR network")
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError as exc:
            raise ConfigError(f"allowed_ips[{index}] is not a valid IP address or CIDR network") from exc
    return tuple(networks)


def load_log_level(path: str) -> str:
    """Load the minimum task log level, defaulting to INFO."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read logging configuration: {exc.__class__.__name__}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("configuration must be a JSON object")
    value = raw.get("log_level", "INFO")
    if not isinstance(value, str) or value.upper() not in LOG_LEVELS:
        raise ConfigError("log_level must be DEBUG, INFO, WARNING, or ERROR")
    return value.upper()


def update_project_branch(path: str, project_id: str, branch: str) -> None:
    """Persist a branch selection in the configured projects JSON file."""
    _validate_branch(branch)
    config_path = Path(path)
    with _CONFIG_WRITE_LOCK:
        try:
            original = config_path.read_bytes()
            data = json.loads(original.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigError("cannot read projects configuration") from exc

        entries = data.get("projects") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise ConfigError("projects configuration must contain a projects array")
        project = next(
            (entry for entry in entries if isinstance(entry, dict) and entry.get("id") == project_id),
            None,
        )
        if project is None:
            raise ConfigError("project is not configured")
        project["branch"] = branch
        updated = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

        try:
            # The config is bind-mounted as a single file, so update its inode
            # in place instead of replacing the mount target with os.replace.
            with config_path.open("r+b") as stream:
                stream.seek(0)
                stream.write(updated)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            try:
                with config_path.open("r+b") as stream:
                    stream.seek(0)
                    stream.write(original)
                    stream.truncate()
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError:
                pass
            raise ConfigError("projects configuration is not writable") from exc


def public_project(
    project: Project,
    status: str = "unknown",
    last_job: dict | None = None,
) -> dict:
    """Return the intentionally public project summary used by the API."""
    return {
        "id": project.id,
        "name": project.name,
        "repository": project.repository,
        "branch": project.branch,
        "workspace": str(project.workspace),
        "workspace_relative": project.workspace_relative,
        "url": project.url,
        "workspace_status": status,
        "last_job": last_job,
    }
