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
        started_at = _utc_now()
        workspace = self._checked_workspace(project.workspace)
        existed = workspace.exists()
        previous_commit: str | None = None

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
                lambda _line: None,
                on_process=on_process,
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
                lambda _line: None,
                on_process=on_process,
                allow_no_match=True,
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
            self._check_git_metadata(workspace, git_directory_path)
            previous_commit = self._run(["-C", str(workspace), "rev-parse", "HEAD"], credentials, on_output, on_process=on_process)

            remote_ref = f"refs/remotes/origin/{project.branch}"
            refspec = f"+refs/heads/{project.branch}:{remote_ref}"
            self._run(["-C", str(workspace), "fetch", "--no-tags", "origin", refspec], credentials, on_output, on_process=on_process)
            untracked = self._run(
                ["-C", str(workspace), "ls-files", "--others", "-z"],
                credentials,
                lambda _line: None,
                on_process=on_process,
                preserve_output=True,
            )
            tracked = self._run(
                ["-C", str(workspace), "ls-tree", "-r", "--name-only", "-z", remote_ref],
                credentials,
                lambda _line: None,
                on_process=on_process,
                preserve_output=True,
            )
            if _has_path_collision(untracked, tracked):
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

    def _check_git_metadata(self, workspace: Path, git_directory: Path) -> None:
        if not git_directory.is_dir() or (git_directory / "commondir").exists():
            raise GitSyncError("linked or shared Git metadata is not supported")
        for current, directories, files in os.walk(git_directory, followlinks=False):
            current_path = Path(current)
            for name in [*directories, *files]:
                entry = current_path / name
                if entry.is_symlink():
                    raise GitSyncError("Git metadata cannot contain symbolic links")
                try:
                    entry.resolve(strict=False).relative_to(workspace)
                except ValueError as exc:
                    raise GitSyncError("Git metadata must remain inside the configured workspace") from exc

    def _run(
        self,
        arguments: list[str],
        credentials: GitCredentials,
        on_output: Callable[[str], None],
        cwd: Path | None = None,
        on_process: Callable[[int | None], None] | None = None,
        allow_no_match: bool = False,
        preserve_output: bool = False,
    ) -> str:
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
        try:
            while not reader_finished:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._terminate(process)
                    reader.join(timeout=2)
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
                if sum(map(len, output_lines)) > 1024 * 1024:
                    output_lines = output_lines[-128:]
                on_output(safe_line)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._terminate(process)
                raise GitSyncError(f"Git command timed out after {self.timeout_seconds:g} seconds")
            try:
                return_code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                self._terminate(process)
                raise GitSyncError(f"Git command timed out after {self.timeout_seconds:g} seconds") from exc
        except Exception:
            self._terminate(process)
            reader.join(timeout=2)
            raise
        finally:
            if on_process:
                on_process(None)
        output = "\n".join(output_lines).strip()
        if return_code == 1 and allow_no_match and not output:
            return ""
        if return_code != 0:
            detail = output[-2000:] if output else f"exit code {return_code}"
            raise GitSyncError(f"Git command failed (exit {return_code}): {detail}")
        if preserve_output:
            return output
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


def _has_path_collision(untracked: str, tracked: str) -> bool:
    untracked_paths = [Path(path) for path in untracked.split("\0") if path]
    tracked_paths = [Path(path) for path in tracked.split("\0") if path]
    return any(
        untracked_path == tracked_path
        or untracked_path in tracked_path.parents
        or tracked_path in untracked_path.parents
        for untracked_path in untracked_paths
        for tracked_path in tracked_paths
    )
