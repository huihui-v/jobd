#!/usr/bin/env python3
"""jobd: thin edge job agent. Python 3 stdlib only."""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import signal
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ALLOWED_ENV = ("ENV_FILE", "ENV_VERSION")
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_BODY = 8 * 1024 * 1024

WRAPPER = r"""#!/bin/bash
machine_env=$1
job_env=$2
user_sh=$3
exit_file=$4
(
  set -e
  set -a
  if [ -f "$machine_env" ]; then
    source "$machine_env"
  fi
  set +a
  set -a
  source "$job_env"
  set +a
  if [ -n "${WORKSPACE_ROOT:-}" ]; then
    cd "$WORKSPACE_ROOT/AIGCTeam_comfy_boot"
  fi
  exec stdbuf -oL -eL bash "$user_sh"
)
ec=$?
printf '%s\n' "$ec" > "$exit_file"
exit "$ec"
"""


def sh_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def wait_status_code(status: int):
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return None


def child_alive(pid: int):
    """Return (alive, exit_code_if_reaped). Reaps if we are the parent."""
    if not pid:
        return False, None
    try:
        wpid, status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return pid_alive(pid), None
    except OSError:
        return pid_alive(pid), None
    if wpid == 0:
        return True, None
    return False, wait_status_code(status)


class JobManager:
    def __init__(self, workdir, machine_env, token):
        self.workdir = Path(workdir)
        self.current = self.workdir / "current"
        self.machine_env = Path(machine_env)
        self.token = token
        self.lock = threading.Lock()
        self.current.mkdir(parents=True, exist_ok=True)

    @property
    def status_path(self) -> Path:
        return self.current / "status.json"

    @property
    def user_sh(self) -> Path:
        return self.current / "user.sh"

    @property
    def job_env(self) -> Path:
        return self.current / "job.env"

    @property
    def job_log(self) -> Path:
        return self.current / "job.log"

    @property
    def exit_file(self) -> Path:
        return self.current / "exit_code"

    @property
    def wrapper_path(self) -> Path:
        return self.current / "wrapper.sh"

    def authorized(self, header: str) -> bool:
        if not header or not header.startswith("Bearer "):
            return False
        got = header[len("Bearer ") :]
        try:
            return hmac.compare_digest(got, self.token)
        except (TypeError, ValueError):
            return False

    def refresh(self):
        if not self.status_path.is_file():
            return None
        try:
            st = json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        pid = int(st["pid"]) if st.get("pid") else 0
        alive, wait_code = child_alive(pid)
        if alive:
            st["status"] = "running"
            st["exit_code"] = None
            return st
        code = self._read_exit_code()
        if code is None:
            code = wait_code
        st["status"] = "exited" if code == 0 else "failed"
        st["exit_code"] = code
        self._write_status(st)
        return st

    def snapshot(self):
        with self.lock:
            return self.refresh()

    def submit(self, script: str, env: dict, name: str, force: bool):
        with self.lock:
            st = self.refresh()
            if st and st.get("status") == "running":
                if not force:
                    return "conflict", st
                self._cancel_locked(st)
            return "ok", self._start_locked(script, env, name)

    def cancel(self):
        with self.lock:
            st = self.refresh()
            if not st or st.get("status") != "running":
                return "missing", None
            self._cancel_locked(st)
            return "ok", self.refresh()

    def read_log(self, offset: int):
        st = self.snapshot()
        if st is None:
            return None
        if not self.job_log.is_file():
            return {"data": "", "offset": 0}
        size = self.job_log.stat().st_size
        if offset > size:
            offset = size
        with open(self.job_log, "rb") as f:
            f.seek(offset)
            chunk = f.read()
        text = chunk.decode("utf-8", "replace")
        return {"data": text, "offset": offset + len(chunk)}

    def _start_locked(self, script: str, env: dict, name: str):
        self.current.mkdir(parents=True, exist_ok=True)
        self.user_sh.write_bytes(script.encode("utf-8"))
        self._write_job_env(env)
        self.wrapper_path.write_text(WRAPPER, encoding="utf-8")
        if self.exit_file.exists():
            self.exit_file.unlink()
        fd = os.open(
            str(self.job_log),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        )
        try:
            proc = subprocess.Popen(
                [
                    "bash",
                    str(self.wrapper_path),
                    str(self.machine_env),
                    str(self.job_env),
                    str(self.user_sh),
                    str(self.exit_file),
                ],
                stdin=subprocess.DEVNULL,
                stdout=fd,
                stderr=fd,
                cwd=str(self.current),
                start_new_session=True,
                close_fds=True,
            )
        finally:
            os.close(fd)
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = proc.pid
        st = {
            "job_id": uuid.uuid4().hex,
            "name": name,
            "status": "running",
            "pid": proc.pid,
            "pgid": pgid,
            "exit_code": None,
            "started_at": utc_now(),
        }
        self._write_status(st)
        return st

    def _write_job_env(self, env: dict) -> None:
        lines = []
        for key, val in env.items():
            if key not in ALLOWED_ENV:
                continue
            if not KEY_RE.match(key):
                continue
            lines.append("export %s=%s\n" % (key, sh_single_quote(str(val))))
        self.job_env.write_text("".join(lines), encoding="utf-8")

    def _cancel_locked(self, st) -> None:
        pid = int(st.get("pid") or 0)
        pgid = int(st.get("pgid") or pid)
        alive, _ = child_alive(pid)
        if alive:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except OSError:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            deadline = time.time() + 2
            while time.time() < deadline:
                alive, _ = child_alive(pid)
                if not alive:
                    break
                time.sleep(0.05)
            if alive:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except OSError:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
                deadline = time.time() + 2
                while time.time() < deadline:
                    alive, _ = child_alive(pid)
                    if not alive:
                        break
                    time.sleep(0.05)
        self.refresh()

    def _read_exit_code(self):
        if not self.exit_file.is_file():
            return None
        raw = self.exit_file.read_text(encoding="utf-8").strip()
        if raw == "":
            return None
        try:
            return int(raw.split()[0])
        except ValueError:
            return None

    def _write_status(self, st) -> None:
        tmp = self.status_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st) + "\n", encoding="utf-8")
        tmp.replace(self.status_path)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(manager: JobManager):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write("jobd: " + (fmt % args) + "\n")

        def do_GET(self):
            if not self._auth():
                return
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            qs = parse_qs(parsed.query)
            if path == "/jobs/current":
                st = manager.snapshot()
                if st is None:
                    self._json(404, {"error": "no job"})
                    return
                self._json(200, public_status(st))
                return
            if path == "/jobs/current/log":
                raw_off = (qs.get("offset") or ["0"])[0] or "0"
                try:
                    offset = int(raw_off)
                except ValueError:
                    self._json(400, {"error": "bad offset"})
                    return
                if offset < 0:
                    self._json(400, {"error": "bad offset"})
                    return
                body = manager.read_log(offset)
                if body is None:
                    self._json(404, {"error": "no job"})
                    return
                self._json(200, body)
                return
            self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._auth():
                return
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            qs = parse_qs(parsed.query)
            if path == "/jobs":
                payload, err = self._read_json()
                if err:
                    self._json(400, {"error": err})
                    return
                script = payload.get("script")
                if not isinstance(script, str):
                    self._json(400, {"error": "script required"})
                    return
                env = payload.get("env") or {}
                if not isinstance(env, dict):
                    self._json(400, {"error": "env must be object"})
                    return
                name = payload.get("name") if isinstance(payload.get("name"), str) else ""
                force = (qs.get("force") or [""])[0] in ("1", "true", "yes")
                kind, st = manager.submit(script, env, name, force)
                if kind == "conflict":
                    body = public_status(st)
                    body["error"] = "job running"
                    self._json(409, body)
                    return
                self._json(200, {"job_id": st["job_id"], "status": "running"})
                return
            if path == "/jobs/current/cancel":
                kind, st = manager.cancel()
                if kind == "missing":
                    self._json(404, {"error": "no live job"})
                    return
                self._json(200, public_status(st) if st else {"status": "failed"})
                return
            self._json(404, {"error": "not found"})

        def _auth(self) -> bool:
            if manager.authorized(self.headers.get("Authorization") or ""):
                return True
            self._json(401, {"error": "unauthorized"})
            return False

        def _read_json(self):
            length = self.headers.get("Content-Length")
            if not length:
                return None, "missing body"
            try:
                n = int(length)
            except ValueError:
                return None, "bad content-length"
            if n < 0 or n > MAX_BODY:
                return None, "body too large"
            raw = self.rfile.read(n)
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None, "invalid json"
            if not isinstance(obj, dict):
                return None, "json object required"
            return obj, None

        def _json(self, code, obj):
            raw = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    return Handler


def public_status(st):
    return {
        "job_id": st.get("job_id"),
        "name": st.get("name") or "",
        "status": st.get("status"),
        "pid": st.get("pid"),
        "exit_code": st.get("exit_code"),
        "started_at": st.get("started_at"),
    }


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="jobd — thin edge job agent")
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18789)
    p.add_argument("--token", required=True)
    p.add_argument("--machine-env", default="/root/.machine.env")
    p.add_argument("--workdir", default="/root/hkpc-job")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if not args.token:
        sys.stderr.write("jobd: --token is required\n")
        return 2
    mgr = JobManager(args.workdir, args.machine_env, args.token)
    httpd = ThreadingHTTPServer((args.bind, args.port), make_handler(mgr))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
