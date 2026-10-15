"""Git sync control panel application package."""

import ipaddress
import os
import secrets
import threading
from flask import Flask, abort, jsonify, render_template, request
from werkzeug.exceptions import HTTPException, InternalServerError

from app.config import load_allowed_ips, load_log_level, load_projects
from app.credentials import GitCredentials
from app.git_client import GitClient
from app.jobs import JobStore
from app.runner import UpdateRunner


def create_app(
    config_path: str | None = None,
    state_dir: str | None = None,
    workspace_root: str | None = None,
    git_client: GitClient | None = None,
    credential_factory=None,
) -> Flask:
    """Create a configured Flask app with isolated, injectable dependencies."""
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config["PAGE_TOKEN"] = secrets.token_urlsafe(32)

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException):
        if request.path.startswith("/api/") or request.path == "/healthz":
            return jsonify({"error": error.description}), error.code
        if error.code == 403:
            return render_template("403.html"), 403
        return error

    @app.errorhandler(Exception)
    def handle_unexpected_error(error: Exception):
        if request.path.startswith("/api/") or request.path == "/healthz":
            app.logger.error("Unhandled API exception (%s)", error.__class__.__name__)
            return jsonify({"error": "internal server error"}), 500
        return InternalServerError()

    config_path = config_path or os.environ.get("APP_CONFIG", "/config/config.json")
    state_dir = state_dir or os.environ.get("STATE_DIR", "/state")
    workspace_root = workspace_root or os.environ.get("WORKSPACE_DIR", "/workspace")

    projects = load_projects(config_path, workspace_root)
    allowed_networks = load_allowed_ips(config_path)
    log_level = load_log_level(config_path)

    @app.before_request
    def enforce_client_ip_allowlist():
        remote_addr = request.remote_addr
        try:
            client_ip = ipaddress.ip_address(remote_addr) if remote_addr else None
        except ValueError:
            client_ip = None
        if isinstance(client_ip, ipaddress.IPv6Address) and client_ip.ipv4_mapped:
            client_ip = client_ip.ipv4_mapped
        if client_ip is None or not any(
            client_ip.version == network.version and client_ip in network
            for network in allowed_networks
        ):
            abort(403, description="client IP is not allowlisted")

    store = JobStore(state_dir)
    client = git_client or GitClient(workspace_root=workspace_root, log_level=log_level)
    credentials = credential_factory or GitCredentials.from_environment
    runner = UpdateRunner(projects, store, client, credential_factory=credentials)
    runner.recover()

    app.extensions["projects"] = projects
    app.extensions["job_store"] = store
    app.extensions["update_runner"] = runner
    app.extensions["git_client"] = client
    app.extensions["credential_factory"] = credentials
    app.extensions["project_config_path"] = config_path
    app.extensions["project_config_lock"] = threading.RLock()

    from app.web import web

    app.register_blueprint(web)
    return app
