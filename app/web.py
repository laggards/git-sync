from __future__ import annotations

import hmac
from dataclasses import replace
from urllib.parse import urlsplit

from flask import Blueprint, abort, current_app, jsonify, render_template, request

from app.config import ConfigError, Project, public_project, update_project_branch
from app.credentials import CredentialError
from app.git_client import GitSyncError
from app.jobs import JobStoreError


web = Blueprint("web", __name__)


@web.get("/")
def index():
    return render_template(
        "index.html",
        page_token=current_app.config["PAGE_TOKEN"],
        projects=_project_summaries(),
    )


@web.get("/api/projects")
def projects():
    return jsonify({"projects": _project_summaries(), "busy": current_app.extensions["update_runner"].busy})


def _project_summaries() -> list[dict]:
    store = current_app.extensions["job_store"]
    runner = current_app.extensions["update_runner"]
    jobs = store.list_recent()
    summaries = []
    for project in current_app.extensions["projects"]:
        last_job = next((job for job in jobs if job["project_id"] == project.id), None)
        status = _workspace_status(project, last_job)
        safe_last_job = None
        if last_job:
            safe_last_job = {
                key: last_job.get(key)
                for key in (
                    "id",
                    "status",
                    "result",
                    "error",
                    "started_at",
                    "finished_at",
                    "duration_seconds",
                    "previous_commit",
                    "commit",
                )
            }
        item = public_project(project, status=status, last_job=safe_last_job)
        item["busy"] = runner.busy and bool(last_job and last_job.get("status") == "running")
        summaries.append(item)
    return summaries


@web.post("/api/projects/<project_id>/update")
def update_project(project_id: str):
    _check_page_token()
    with current_app.extensions["project_config_lock"]:
        status, result = current_app.extensions["update_runner"].start(project_id)
    if status == 202:
        return jsonify({"job": result}), status
    return jsonify(result), status


@web.get("/api/projects/<project_id>/branches")
def list_project_branches(project_id: str):
    _check_page_token()
    project = current_app.extensions["update_runner"].projects.get(project_id)
    if project is None:
        abort(404, description="project is not configured")
    try:
        branches = _list_remote_branches(project)
    except (CredentialError, GitSyncError) as exc:
        return jsonify({"error": str(exc)}), 502
    return jsonify({"branches": branches, "current_branch": project.branch})


@web.post("/api/projects/<project_id>/branch")
def change_project_branch(project_id: str):
    _check_page_token()
    payload = request.get_json(silent=True)
    branch = payload.get("branch") if isinstance(payload, dict) else None
    if not isinstance(branch, str) or not branch:
        return jsonify({"error": "branch is required"}), 400

    lock = current_app.extensions["project_config_lock"]
    with lock:
        runner = current_app.extensions["update_runner"]
        if runner.busy:
            return jsonify({"error": "cannot change branch while a sync is running"}), 409
        project = runner.projects.get(project_id)
        if project is None:
            return jsonify({"error": "project is not configured"}), 404
        try:
            branches = _list_remote_branches(project)
        except (CredentialError, GitSyncError) as exc:
            return jsonify({"error": str(exc)}), 502
        if branch not in branches:
            return jsonify({"error": "branch is not available in the remote repository"}), 400
        try:
            update_project_branch(
                current_app.extensions["project_config_path"],
                project_id,
                branch,
            )
        except ConfigError as exc:
            return jsonify({"error": str(exc)}), 500

        updated_project = replace(project, branch=branch)
        runner.projects[project_id] = updated_project
        projects = current_app.extensions["projects"]
        projects[:] = [updated_project if item.id == project_id else item for item in projects]
    return jsonify({"project_id": project_id, "branch": branch})


def _list_remote_branches(project: Project) -> list[str]:
    credential_factory = current_app.extensions["credential_factory"]
    with credential_factory() as credentials:
        return current_app.extensions["git_client"].list_branches(project, credentials)


@web.get("/api/jobs/<job_id>")
def get_job(job_id: str):
    job = current_app.extensions["job_store"].get(job_id)
    if job is None:
        abort(404)
    public_fields = {
        key: value
        for key, value in job.items()
        if key != "process_id"
    }
    public_fields["log"] = current_app.extensions["job_store"].get_log(job_id)
    return jsonify({"job": public_fields})


@web.post("/api/jobs/clear")
def clear_jobs():
    _check_page_token()
    with current_app.extensions["project_config_lock"]:
        runner = current_app.extensions["update_runner"]
        if runner.busy:
            return jsonify({"error": "cannot clear logs while a sync is running"}), 409
        try:
            current_app.extensions["job_store"].clear()
        except JobStoreError as exc:
            return jsonify({"error": str(exc)}), 500
    return jsonify({"cleared": True})


@web.get("/api/jobs")
def list_jobs():
    store = current_app.extensions["job_store"]
    revision = store.revision
    if request.if_none_match.contains(revision):
        return "", 304, {"ETag": f'"{revision}"', "Cache-Control": "no-cache"}

    jobs = []
    for job in store.list_recent():
        public_job = {key: value for key, value in job.items() if key != "process_id"}
        public_job["log"] = store.get_log(job["id"])
        jobs.append(public_job)
    response = jsonify({"jobs": jobs})
    response.set_etag(revision)
    response.headers["Cache-Control"] = "no-cache"
    return response


@web.get("/healthz")
def healthz():
    return jsonify({"status": "ok"})


def _check_page_token() -> None:
    supplied = request.headers.get("X-Page-Token", "")
    expected = current_app.config["PAGE_TOKEN"]
    if not supplied or not hmac.compare_digest(supplied, expected):
        abort(403, description="invalid page token")

    fetch_site = request.headers.get("Sec-Fetch-Site", "").lower()
    if fetch_site == "cross-site":
        abort(403, description="cross-site request rejected")

    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if origin:
        try:
            origin_host = urlsplit(origin).netloc.lower()
        except ValueError:
            abort(403, description="invalid request origin")
        if not origin_host or origin_host != request.host.lower():
            abort(403, description="cross-site request rejected")


def _workspace_status(project: Project, last_job: dict | None) -> str:
    if last_job and last_job.get("status") == "running":
        return "updating"
    if not project.workspace.exists():
        return "not_initialized"
    if not project.workspace.is_dir() or not (project.workspace / ".git").exists():
        return "invalid"
    return "ready"
