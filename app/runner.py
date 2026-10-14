from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable

from app.credentials import GitCredentials
from app.git_client import GitClient, SyncResult
from app.jobs import JobStore


class UpdateRunner:
    def __init__(
        self,
        projects: list,
        store: JobStore,
        git_client: GitClient,
        credential_factory: Callable[[], GitCredentials] | None = None,
        process_checker: Callable[[int], bool] | None = None,
        recovery_poll_seconds: float = 1.0,
    ):
        self.projects = {project.id: project for project in projects}
        self.store = store
        self.git_client = git_client
        self.credential_factory = credential_factory or GitCredentials.from_environment
        self.process_checker = process_checker or _process_alive
        self.recovery_poll_seconds = recovery_poll_seconds
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def start(self, project_id: str) -> tuple[int, dict]:
        project = self.projects.get(project_id)
        if project is None:
            return 404, {"error": "project is not configured"}
        if not self._lock.acquire(blocking=False):
            return 409, {"error": "another update is already running"}
        try:
            job = self.store.create(
                project.id,
                project.name,
                project.repository,
                project.branch,
                str(project.workspace),
            )
        except Exception:
            self._lock.release()
            raise
        thread = threading.Thread(target=self._execute, args=(project, job["id"]), daemon=True)
        self._thread = thread
        try:
            thread.start()
        except Exception:
            self.store.finish(job["id"], "failed", error="failed to start update worker")
            self._lock.release()
            raise
        return 202, job

    def recover(self) -> None:
        running = [job for job in self.store.list_recent() if job.get("status") == "running"]
        if not running:
            return
        active: list[tuple[dict, int]] = []
        for job in running:
            self.store.append_log(job["id"], "日志采集中断：面板在任务完成前重新启动。")
            process_id = job.get("process_id")
            if isinstance(process_id, int) and process_id > 0 and self.process_checker(process_id):
                active.append((job, process_id))
            else:
                self.store.finish(job["id"], "failed", error="面板重启导致同步中断")
        if not active:
            return
        self._lock.acquire()
        thread = threading.Thread(target=self._wait_for_recovered_processes, args=(active,), daemon=True)
        self._thread = thread
        thread.start()

    def wait_until_idle(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while self.busy and time.monotonic() < deadline:
            time.sleep(0.01)
        return not self.busy

    def _execute(self, project, job_id: str) -> None:
        try:
            with self.credential_factory() as credentials:
                result: SyncResult = self.git_client.sync(
                    project,
                    credentials,
                    lambda line: self.store.append_log(job_id, line),
                    lambda process_id: self.store.set_process_id(job_id, process_id),
                )
            self.store.set_commits(job_id, result.previous_commit, result.commit)
            self.store.finish(job_id, "succeeded", result="同步成功")
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            self.store.append_log(job_id, f"同步失败：{message}")
            self.store.finish(job_id, "failed", result="同步失败", error=message)
        finally:
            self._lock.release()

    def _wait_for_recovered_processes(self, active: list[tuple[dict, int]]) -> None:
        remaining = list(active)
        try:
            while remaining:
                for job, process_id in list(remaining):
                    if not self.process_checker(process_id):
                        self.store.finish(job["id"], "failed", error="面板重启期间同步进程已退出")
                        remaining.remove((job, process_id))
                if remaining:
                    time.sleep(self.recovery_poll_seconds)
        finally:
            self._lock.release()


def _process_alive(process_id: int) -> bool:
    try:
        if os.name == "nt":
            os.kill(process_id, 0)
        else:
            os.killpg(process_id, 0)
    except (OSError, ProcessLookupError):
        return False
    return True
