from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


MAX_LOG_BYTES = 1024 * 1024
MAX_JOBS = 100


class JobStoreError(RuntimeError):
    """Raised when persistent job state cannot be read or written."""


class JobStore:
    def __init__(self, state_dir: str):
        self.directory = Path(state_dir)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "jobs.json"
        self.log_directory = self.directory / "logs"
        self.log_directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._revision_id = uuid.uuid4().hex
        self._revision_number = 0
        self._needs_cleanup = False
        self._jobs = self._load()
        if self._needs_cleanup:
            self._save()
        self._prune_logs()

    @property
    def revision(self) -> str:
        with self._lock:
            return f"{self._revision_id}-{self._revision_number}"

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise JobStoreError("cannot read persisted job state") from exc
        if not isinstance(value, list) or any(not isinstance(job, dict) for job in value):
            raise JobStoreError("persisted job state has an invalid format")
        jobs = sorted(value, key=_job_time_key, reverse=True)[:MAX_JOBS]
        original_ids = [job.get("id") for job in value]
        retained_ids = [job.get("id") for job in jobs]
        self._needs_cleanup = (
            len(value) != len(jobs)
            or original_ids != retained_ids
            or any("log" in job for job in value)
        )
        # Historical inline logs are discarded rather than migrated.
        for job in jobs:
            job.pop("log", None)
        return jobs

    def _save(self) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(self._jobs[:MAX_JOBS], stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
            self._revision_number += 1
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise JobStoreError("cannot persist job state") from exc

    def create(self, project_id: str, project_name: str, repository: str, branch: str, workspace: str) -> dict:
        now = _utc_now()
        job = {
            "id": uuid.uuid4().hex,
            "project_id": project_id,
            "project_name": project_name,
            "repository": repository,
            "branch": branch,
            "workspace": workspace,
            "status": "running",
            "result": None,
            "error": None,
            "log_truncated": False,
            "started_at": now,
            "finished_at": None,
            "duration_seconds": None,
            "previous_commit": None,
            "commit": None,
            "process_id": None,
        }
        with self._lock:
            self._jobs.insert(0, job)
            self._jobs.sort(key=_job_time_key, reverse=True)
            self._jobs = self._jobs[:MAX_JOBS]
            self._save()
            self._prune_logs()
        return _copy(job)

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            for job in self._jobs:
                if job.get("id") == job_id:
                    return _copy(job)
        return None

    def list_recent(self) -> list[dict]:
        with self._lock:
            return [_copy(job) for job in self._jobs]

    def clear(self) -> None:
        with self._lock:
            self._jobs = []
            self._save()
            try:
                for path in self.log_directory.glob("*.log"):
                    path.unlink()
            except OSError as exc:
                raise JobStoreError("cannot clear persisted job logs") from exc

    def get_log(self, job_id: str) -> str:
        path = self._log_path(job_id)
        if path is None:
            return ""
        with self._lock:
            try:
                return path.read_text(encoding="utf-8") if path.exists() else ""
            except (OSError, UnicodeError) as exc:
                raise JobStoreError("cannot read persisted job log") from exc

    def append_log(self, job_id: str, text: str) -> None:
        if not text:
            return
        with self._lock:
            job = self._find(job_id)
            path = self._log_path(job_id)
            if path is None:
                raise JobStoreError("invalid job id for log storage")
            created = not path.exists()
            try:
                with path.open("ab") as stream:
                    if stream.tell() > 0:
                        stream.write(b"\n")
                    stream.write(text.encode("utf-8"))
                    size = stream.tell()
                if created:
                    try:
                        path.chmod(0o600)
                    except OSError:
                        pass
            except OSError as exc:
                raise JobStoreError("cannot append to persisted job log") from exc

            was_truncated = bool(job.get("log_truncated"))
            if size > MAX_LOG_BYTES:
                marker = "[日志已截断，仅保留末尾内容]\n".encode("utf-8")
                budget = MAX_LOG_BYTES - len(marker)
                try:
                    with path.open("rb") as stream:
                        stream.seek(max(0, size - budget))
                        tail = stream.read().decode("utf-8", errors="ignore").encode("utf-8")
                    path.write_bytes(marker + tail)
                except OSError as exc:
                    raise JobStoreError("cannot truncate persisted job log") from exc
                job["log_truncated"] = True

            if job.get("log_truncated") and not was_truncated:
                self._save()
            else:
                self._revision_number += 1

    def _log_path(self, job_id: str) -> Path | None:
        if (
            not isinstance(job_id, str)
            or len(job_id) != 32
            or any(char not in "0123456789abcdef" for char in job_id)
        ):
            return None
        return self.log_directory / f"{job_id}.log"

    def _prune_logs(self) -> None:
        retained_ids = {job.get("id") for job in self._jobs}
        try:
            for path in self.log_directory.glob("*.log"):
                if path.stem not in retained_ids:
                    path.unlink()
        except OSError as exc:
            raise JobStoreError("cannot remove expired job logs") from exc

    def set_process_id(self, job_id: str, process_id: int | None) -> None:
        with self._lock:
            self._find(job_id)["process_id"] = process_id
            self._save()

    def set_commits(self, job_id: str, previous_commit: str | None, commit: str | None) -> None:
        with self._lock:
            job = self._find(job_id)
            job["previous_commit"] = previous_commit
            job["commit"] = commit
            self._save()

    def finish(self, job_id: str, status: str, result: str | None = None, error: str | None = None) -> dict:
        with self._lock:
            job = self._find(job_id)
            finished = datetime.now(timezone.utc)
            started = datetime.fromisoformat(job["started_at"])
            job.update(
                {
                    "status": status,
                    "result": result,
                    "error": error,
                    "process_id": None,
                    "finished_at": finished.isoformat(),
                    "duration_seconds": round(max(0.0, (finished - started).total_seconds()), 3),
                }
            )
            self._save()
            return _copy(job)

    def _find(self, job_id: str) -> dict:
        for job in self._jobs:
            if job.get("id") == job_id:
                return job
        raise KeyError(job_id)


def _copy(value: dict) -> dict:
    return json.loads(json.dumps(value))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_time_key(job: dict) -> float:
    value = job.get("started_at")
    if not isinstance(value, str):
        return float("-inf")
    try:
        started = datetime.fromisoformat(value)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return started.timestamp()
    except (ValueError, OverflowError):
        return float("-inf")
