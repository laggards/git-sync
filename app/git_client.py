from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.config import Project
from app.credentials import GitCredentials


class GitSyncError(RuntimeError):
    """A safe-to-display Git operation failure."""


@dataclass(frozen=True, slots=True)
class SyncResult:
    previous_commit: str | None
    commit: str
    started_at: str
    finished_at: str


class GitClient:
    def __init__(
        self,
        binary: str = "git",
        timeout_seconds: float = 300,
        workspace_root: str | Path = "/workspace",
        popen_factory: Callable = subprocess.Popen,
    ):
        self.binary = binary
        self.timeout_seconds = timeout_seconds
        self.workspace_root = Path(workspace_root)
        self._popen = popen_factory

    def list_branches(self, project: Project, credentials: GitCredentials) -> list[str]:
        output = self._run(
            ["ls-remote", "--heads", "--", project.repository],
            credentials,
            lambda _line: None,
            preserve_output=True,
        )
        branches = []
        for line in output.splitlines():
            _, separator, ref = line.partition("\t")
            if separator and ref.startswith("refs/heads/"):
                branches.append(ref.removeprefix("refs/heads/"))
        return branches

    def sync(
        self,
        project: Project,
        credentials: GitCredentials,
        on_output: Callable[[str], None],
        on_process: Callable[[int | None], None] | None = None,
    ) -> SyncResult:
        sync_started = time.monotonic()
        _emit(on_output, f"[sync] start project={project.id} branch={project.branch} workspace={project.workspace}")
        try:
            result = self._sync(project, credentials, on_output, on_process)
        except Exception as exc:
            _emit(
                on_output,
                f"[sync] failed duration_ms={_elapsed_ms(sync_started)} error_type={exc.__class__.__name__}",
            )
            raise
        _emit(
            on_output,
            f"[sync] succeeded duration_ms={_elapsed_ms(sync_started)} "
            f"previous_commit={result.previous_commit or 'none'} commit={result.commit}",
        )
        return result

    def _sync(
        self,
        project: Project,
        credentials: GitCredentials,
        on_output: Callable[[str], None],
        on_process: Callable[[int | None], None] | None = None,
    ) -> SyncResult:
        started_at = _utc_now()
        workspace = self._checked_workspace(project.workspace)
        existed = workspace.exists()
        previous_commit: str | None = None
        _emit(on_output, f"[sync] workspace_mode={'update' if existed else 'clone'}")

        if existed:
            if not workspace.is_dir():
                raise GitSyncError("configured workspace exists and is not a directory")
            if not (workspace / ".git").exists():
                raise GitSyncError("configured workspace exists but is not a Git repository")
            top_level = self._run(["-C", str(workspace), "rev-parse", "--show-toplevel"], credentials, on_output, on_process=on_process)
            if _resolved(Path(top_level)) != workspace:
                raise GitSyncError("workspace is not the root of a Git repository")
            origin = self._run(
                ["-C", str(workspace), "remote", "get-url", "origin"],
                credentials,
                on_output,
                on_process=on_process,
                stream_output=False,
            )
            if origin != project.repository:
                raise GitSyncError("configured repository origin does not match workspace origin")
            unsafe_config = self._run(
                [
                    "-C",
                    str(workspace),
                    "config",
                    "--local",
                    "--includes",
                    "--get-regexp",
                    r"^(filter\..*\.(smudge|process|clean)|remote\.origin\.(uploadpack|receivepack|vcs)|credential\.helper|include(if)?\..*|extensions\.worktreeconfig)$",
                ],
                credentials,
                on_output,
                on_process=on_process,
                allow_no_match=True,
                stream_output=False,
            )
            if unsafe_config:
                raise GitSyncError("workspace config contains an unsupported executable Git setting")
            git_directory = self._run(
                ["-C", str(workspace), "rev-parse", "--absolute-git-dir"],
                credentials,
                on_output,
                on_process=on_process,
            )
            git_directory_path = _resolved(Path(git_directory))
            try:
                git_directory_path.relative_to(workspace)
            except ValueError as exc:
                raise GitSyncError("Git metadata must remain inside the configured workspace") from exc
            self._timed_stage(
                on_output,
                "git_metadata_check",
                lambda: self._check_git_metadata(git_directory_path, on_output),
            )
            previous_commit = self._run(["-C", str(workspace), "rev-parse", "HEAD"], credentials, on_output, on_process=on_process)

            remote_ref = f"refs/remotes/origin/{project.branch}"
            refspec = f"+refs/heads/{project.branch}:{remote_ref}"
            self._run(
                ["-C", str(workspace), "fetch", "--progress", "--no-tags", "origin", refspec],
                credentials,
                on_output,
                on_process=on_process,
            )
            inventory_started = time.monotonic()
            untracked = self._run(
                ["-C", str(workspace), "ls-files", "--others", "-z"],
                credentials,
                on_output,
                on_process=on_process,
                preserve_output=True,
                stream_output=False,
            )
            untracked_count = _path_count(untracked)
            _emit(on_output, f"[sync] untracked_inventory entries={untracked_count} duration_ms={_elapsed_ms(inventory_started)}")
            inventory_started = time.monotonic()
            tracked = self._run(
                ["-C", str(workspace), "ls-tree", "-r", "--name-only", "-z", remote_ref],
                credentials,
                on_output,
                on_process=on_process,
                preserve_output=True,
                stream_output=False,
            )
            tracked_count = _path_count(tracked)
            _emit(on_output, f"[sync] remote_inventory entries={tracked_count} duration_ms={_elapsed_ms(inventory_started)}")
            collision_started = time.monotonic()
            has_collision = _has_path_collision(untracked, tracked)
            _emit(
                on_output,
                f"[sync] collision_check result={'collision' if has_collision else 'clear'} "
                f"untracked_entries={untracked_count} remote_entries={tracked_count} "
                f"duration_ms={_elapsed_ms(collision_started)}",
            )
            if has_collision:
                raise GitSyncError("untracked files would be overwritten by the remote branch; move them before syncing")
            self._run(
                ["-C", str(workspace), "checkout", "--force", "-B", project.branch, remote_ref],
                credentials,
                on_output,
                on_process=on_process,
            )
            self._run(["-C", str(workspace), "reset", "--hard", remote_ref], credentials, on_output, on_process=on_process)
        else:
            parent = workspace.parent
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise GitSyncError("could not create configured workspace parent directory") from exc
            self._run(
                [
                    "clone",
                    "--single-branch",
                    "--no-tags",
                    "--branch",
                    project.branch,
                    "--",
                    project.repository,
                    str(workspace),
                ],
                credentials,
                on_output,
                cwd=parent,
                on_process=on_process,
            )

        commit = self._run(["-C", str(workspace), "rev-parse", "HEAD"], credentials, on_output, on_process=on_process)
        remote_commit = self._run(
            ["-C", str(workspace), "rev-parse", f"refs/remotes/origin/{project.branch}"],
            credentials,
            on_output,
            on_process=on_process,
        )
        if commit != remote_commit:
            raise GitSyncError("workspace HEAD does not match the configured remote branch")

        return SyncResult(previous_commit, commit, started_at, _utc_now())

    def _checked_workspace(self, workspace: Path) -> Path:
        root = _resolved(self.workspace_root)
        configured = Path(os.path.normcase(os.path.abspath(workspace)))
        candidate = _resolved(workspace)
        if candidate != configured:
            raise GitSyncError("configured workspace cannot pass through symbolic links")
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise GitSyncError("configured workspace resolves outside the mounted workspace root") from exc
        if candidate == root:
            raise GitSyncError("configured workspace cannot be the workspace root")
        return candidate

    def _check_git_metadata(
        self,
        git_directory: Path,
        on_output: Callable[[str], None] | None = None,
    ) -> int:
        if not git_directory.is_dir() or (git_directory / "commondir").exists():
            raise GitSyncError("linked or shared Git metadata is not supported")
        checked_entries = 0
        for current, directories, files in os.walk(git_directory, followlinks=False):
            current_path = Path(current)
            for name in [*directories, *files]:
                entry = current_path / name
                if entry.is_symlink():
                    raise GitSyncError("Git metadata cannot contain symbolic links")
                # The workspace and Git directory were canonicalized and checked
                # to be inside the workspace before this walk. Rejecting every
                # symlink here keeps descendants contained without resolving
                # every object path individually.
                checked_entries += 1
        if on_output:
            _emit(on_output, f"[sync] git_metadata entries_checked={checked_entries}")
        return checked_entries

    @staticmethod
    def _timed_stage(on_output: Callable[[str], None], name: str, action: Callable):
        started = time.monotonic()
        _emit(on_output, f"[stage] {name} started")
        try:
            result = action()
        except Exception as exc:
            _emit(
                on_output,
                f"[stage] {name} failed duration_ms={_elapsed_ms(started)} error_type={exc.__class__.__name__}",
            )
            raise
        _emit(on_output, f"[stage] {name} completed duration_ms={_elapsed_ms(started)}")
        return result

    def _run(
        self,
        arguments: list[str],
        credentials: GitCredentials,
        on_output: Callable[[str], None],
        cwd: Path | None = None,
        on_process: Callable[[int | None], None] | None = None,
        allow_no_match: bool = False,
        preserve_output: bool = False,
        stream_output: bool = True,
    ) -> str:
        command_started = time.monotonic()
        command_name = _git_command_name(arguments)
        _emit(on_output, f"[git] command={command_name} started timeout_seconds={self.timeout_seconds:g}")
        environment = os.environ.copy()
        for name in list(environment):
            if name.startswith("GIT_"):
                environment.pop(name, None)
        environment.update(credentials.git_environment)
        kwargs = {
            "cwd": str(cwd) if cwd else None,
            "env": environment,
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
            "shell": False,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True

        try:
            if os.name == "nt":
                kwargs["stdin"] = subprocess.DEVNULL
                process = self._popen([self.binary, *arguments], **kwargs)
            else:
                launcher = Path(__file__).with_name("git_exec.py")
                process = self._popen([sys.executable, str(launcher), self.binary, *arguments], **kwargs)
        except OSError as exc:
            _emit(on_output, f"[git] command={command_name} spawn_failed duration_ms={_elapsed_ms(command_started)}")
            raise GitSyncError(f"could not start Git CLI: {exc.strerror or exc.__class__.__name__}") from exc
        if on_process:
            try:
                on_process(process.pid)
            except Exception as exc:
                self._terminate(process)
                raise GitSyncError("could not persist active Git process state") from exc
        if os.name != "nt":
            try:
                assert process.stdin is not None
                process.stdin.write("go\n")
                process.stdin.flush()
                process.stdin.close()
            except (BrokenPipeError, OSError) as exc:
                self._terminate(process)
                raise GitSyncError("could not start Git command after recording process state") from exc

        output_lines: list[str] = []
        output_characters = 0
        output_queue: queue.Queue[str | None] = queue.Queue(maxsize=256)

        def read_output() -> None:
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    while True:
                        try:
                            output_queue.put(line, timeout=0.1)
                            break
                        except queue.Full:
                            if process.poll() is not None:
                                break
            finally:
                while True:
                    try:
                        output_queue.put(None, timeout=0.1)
                        break
                    except queue.Full:
                        if process.poll() is not None:
                            continue

        reader = threading.Thread(target=read_output, name="git-output-reader", daemon=True)
        reader.start()
        deadline = time.monotonic() + self.timeout_seconds
        reader_finished = False
        last_progress_log = 0.0
        pending_progress: str | None = None
        try:
            while not reader_finished:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._terminate(process)
                    reader.join(timeout=2)
                    _emit(on_output, f"[git] command={command_name} timed_out duration_ms={_elapsed_ms(command_started)}")
                    raise GitSyncError(f"Git command timed out after {self.timeout_seconds:g} seconds")
                try:
                    line = output_queue.get(timeout=min(0.1, remaining))
                except queue.Empty:
                    if process.poll() is not None and not reader.is_alive():
                        break
                    continue
                if line is None:
                    reader_finished = True
                    continue
                safe_line = credentials.redact(line.rstrip("\r\n"))
                output_lines.append(safe_line)
                output_characters += len(safe_line) + 1
                if not preserve_output and output_characters > 1024 * 1024:
                    removed = output_lines[:-128]
                    output_characters -= sum(len(item) + 1 for item in removed)
                    output_lines = output_lines[-128:]
                if _is_git_progress(safe_line):
                    if not stream_output:
                        continue
                    now = time.monotonic()
                    pending_progress = safe_line
                    if now - last_progress_log >= 2.0:
                        on_output(f"[git progress] {safe_line}")
                        last_progress_log = now
                        pending_progress = None
                else:
                    if not stream_output:
                        continue
                    if pending_progress:
                        on_output(f"[git progress] {pending_progress}")
                        pending_progress = None
                    on_output(safe_line)
                    if "unable to unlink old '" in safe_line.lower() and "permission denied" in safe_line.lower():
                        on_output(
                            "[diagnostic] Git could not replace a workspace file; check the file and parent "
                            "directory owner/write permissions, read-only mounts, and ACLs."
                        )

            if pending_progress:
                on_output(f"[git progress] {pending_progress}")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._terminate(process)
                _emit(on_output, f"[git] command={command_name} timed_out duration_ms={_elapsed_ms(command_started)}")
                raise GitSyncError(f"Git command timed out after {self.timeout_seconds:g} seconds")
            try:
                return_code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                self._terminate(process)
                _emit(on_output, f"[git] command={command_name} timed_out duration_ms={_elapsed_ms(command_started)}")
                raise GitSyncError(f"Git command timed out after {self.timeout_seconds:g} seconds") from exc
        except Exception:
            self._terminate(process)
            reader.join(timeout=2)
            raise
        finally:
            if on_process:
                on_process(None)
        output_text = "\n".join(output_lines)
        output = output_text.strip()
        if return_code == 1 and allow_no_match and not output:
            _emit(on_output, f"[git] command={command_name} completed exit_code=1 outcome=no_match duration_ms={_elapsed_ms(command_started)}")
            return ""
        if return_code != 0:
            _emit(on_output, f"[git] command={command_name} failed exit_code={return_code} duration_ms={_elapsed_ms(command_started)}")
            detail = output[-2000:] if output else f"exit code {return_code}"
            raise GitSyncError(f"Git command failed (exit {return_code}): {detail}")
        _emit(on_output, f"[git] command={command_name} completed exit_code=0 duration_ms={_elapsed_ms(command_started)}")
        if preserve_output:
            return output_text
        return output.splitlines()[-1].strip() if output else ""

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if os.name == "nt":
            if process.poll() is not None:
                process.wait()
                return
            try:
                process.terminate()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait()
            return

        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
        process.wait()


def _resolved(path: Path) -> Path:
    return Path(os.path.normcase(str(path.resolve(strict=False))))


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _emit(on_output: Callable[[str], None], message: str) -> None:
    on_output(f"[{_utc_now()}] {message}")


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _path_count(output: str) -> int:
    return output.count("\0")


def _git_command_name(arguments: list[str]) -> str:
    values = list(arguments)
    while values and values[0] == "-C":
        values = values[2:]
    return values[0] if values else "unknown"


def _is_git_progress(line: str) -> bool:
    progress_prefixes = (
        "enumerating objects:",
        "counting objects:",
        "compressing objects:",
        "receiving objects:",
        "resolving deltas:",
        "updating files:",
        "checking connectivity:",
        "remote: enumerating objects:",
        "remote: counting objects:",
        "remote: compressing objects:",
        "remote: receiving objects:",
        "remote: resolving deltas:",
    )
    return line.lower().startswith(progress_prefixes)


def _has_path_collision(untracked: str, tracked: str) -> bool:
    untracked_paths = (path for path in untracked.split("\0") if path)
    tracked_paths = {path for path in tracked.split("\0") if path}
    tracked_parents: set[str] = set()
    for path in tracked_paths:
        parent = path
        while "/" in parent:
            parent = parent.rsplit("/", 1)[0]
            tracked_parents.add(parent)

    for path in untracked_paths:
        if path in tracked_paths or path in tracked_parents:
            return True
        parent = path
        while "/" in parent:
            parent = parent.rsplit("/", 1)[0]
            if parent in tracked_paths:
                return True
    return False
