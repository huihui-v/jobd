#!/usr/bin/env python3
"""Acceptance tests for jobd. Stdlib only."""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JOBD = ROOT / "jobd.py"
STDBUF_DIR = ROOT / "tests" / "bin"
TOKEN = "test-token-jobd"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def http(method: str, url: str, body=None, token=TOKEN, timeout=5):
    data = None
    headers = {}
    if token is not None:
        headers["Authorization"] = "Bearer %s" % token
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
            return resp.status, parsed, dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            parsed = {"raw": raw.decode("utf-8", "replace")}
        return e.code, parsed, dict(e.headers)


def wait_listen(port: int, proc: subprocess.Popen, timeout=5) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("jobd exited early with %s" % proc.returncode)
        try:
            with socket.create_connection(("127.0.0.1", port), 0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError("jobd not listening on %s" % port)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class JobdTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="jobd-test-")
        self.workdir = Path(self.tmpdir) / "hkpc-job"
        self.machine_env = Path(self.tmpdir) / "machine.env"
        self.ws_root = Path(self.tmpdir) / "ws"
        boot = self.ws_root / "AIGCTeam_comfy_boot"
        boot.mkdir(parents=True)
        self.machine_env.write_text(
            "export WORKSPACE_ROOT='%s'\n" % self.ws_root.as_posix(),
            encoding="utf-8",
        )
        self.port = free_port()
        self.base = "http://127.0.0.1:%d" % self.port
        self.proc = self._start_jobd()

    def _start_jobd(self) -> subprocess.Popen:
        env = os.environ.copy()
        env["PATH"] = str(STDBUF_DIR) + os.pathsep + env.get("PATH", "")
        proc = subprocess.Popen(
            [
                sys.executable,
                str(JOBD),
                "--bind",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--token",
                TOKEN,
                "--machine-env",
                str(self.machine_env),
                "--workdir",
                str(self.workdir),
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        wait_listen(self.port, proc)
        return proc

    def tearDown(self):
        try:
            http("POST", self.base + "/jobs/current/cancel")
        except Exception:
            pass
        if getattr(self, "proc", None) and self.proc.poll() is None:
            try:
                os.kill(self.proc.pid, signal.SIGKILL)
            except OSError:
                pass
            self.proc.wait(timeout=5)
        # leftover user jobs
        status = self.workdir / "current" / "status.json"
        if status.is_file():
            try:
                st = json.loads(status.read_text())
                pid = st.get("pid")
                pgid = st.get("pgid") or pid
                if pid and pid_alive(int(pid)):
                    os.killpg(int(pgid), signal.SIGKILL)
            except Exception:
                pass

    def post_job(self, script, env=None, name="t", force=False, token=TOKEN):
        path = "/jobs"
        if force:
            path += "?force=1"
        body = {"script": script, "name": name}
        if env is not None:
            body["env"] = env
        return http("POST", self.base + path, body=body, token=token)

    def wait_log_contains(self, needle: str, timeout=8):
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            code, body, _ = http("GET", self.base + "/jobs/current/log?offset=0")
            if code == 200:
                last = body.get("data", "")
                if needle in last:
                    return last
            time.sleep(0.05)
        self.fail("log did not contain %r; last=%r" % (needle, last))

    def wait_status(self, wanted, timeout=8):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            code, body, _ = http("GET", self.base + "/jobs/current")
            last = (code, body)
            if code == 200 and body.get("status") == wanted:
                return body
            time.sleep(0.05)
        self.fail("status != %s; last=%s" % (wanted, last))

    def test_no_token_is_401(self):
        code, body, _ = http("GET", self.base + "/jobs/current", token=None)
        self.assertEqual(code, 401)
        code, _, _ = http("GET", self.base + "/jobs/current", token="wrong")
        self.assertEqual(code, 401)

    def test_workspace_root_from_machine_env(self):
        code, body, _ = self.post_job("echo \"$WORKSPACE_ROOT\"\n")
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "running")
        log = self.wait_log_contains(self.ws_root.as_posix())
        self.assertIn(self.ws_root.as_posix(), log.splitlines())
        st = self.wait_status("exited")
        self.assertEqual(st["exit_code"], 0)

    def test_env_version_exported_and_inline(self):
        script = (
            "echo \"$ENV_VERSION\"\n"
            "ENV_VERSION=$ENV_VERSION sh -c 'echo inline:$ENV_VERSION'\n"
        )
        code, _, _ = self.post_job(script, env={"ENV_VERSION": "bbb-v2"})
        self.assertEqual(code, 200)
        log = self.wait_log_contains("bbb-v2")
        self.assertIn("bbb-v2", log.splitlines())
        self.assertIn("inline:bbb-v2", log.splitlines())
        job_env = (self.workdir / "current" / "job.env").read_text(encoding="utf-8")
        self.assertIn("ENV_VERSION", job_env)
        self.assertNotIn("WORKSPACE_ROOT", job_env)

    def test_jobd_death_does_not_kill_job_and_restart_resumes(self):
        script = (
            "echo started\n"
            "sleep 30\n"
            "echo done\n"
        )
        code, body, _ = self.post_job(script, name="persist")
        self.assertEqual(code, 200)
        self.wait_log_contains("started")
        st_code, st, _ = http("GET", self.base + "/jobs/current")
        self.assertEqual(st_code, 200)
        job_pid = int(st["pid"])
        job_id = st["job_id"]
        self.assertTrue(pid_alive(job_pid))

        os.kill(self.proc.pid, signal.SIGKILL)
        self.proc.wait(timeout=5)
        self.assertTrue(pid_alive(job_pid), "user job died with jobd")

        self.proc = self._start_jobd()
        code, st2, _ = http("GET", self.base + "/jobs/current")
        self.assertEqual(code, 200)
        self.assertEqual(st2["job_id"], job_id)
        self.assertEqual(st2["status"], "running")
        self.assertEqual(int(st2["pid"]), job_pid)
        code, logbody, _ = http("GET", self.base + "/jobs/current/log?offset=0")
        self.assertEqual(code, 200)
        self.assertIn("started", logbody.get("data", ""))
        self.assertIn("offset", logbody)

    def test_second_post_while_running_is_409(self):
        code, first, _ = self.post_job("sleep 30\necho end\n")
        self.assertEqual(code, 200)
        self.wait_status("running")
        code, body, _ = self.post_job("echo should-not-run\n", name="second")
        self.assertEqual(code, 409)
        self.assertEqual(body["job_id"], first["job_id"])

    def test_cancel_kills_process_group(self):
        child_pid_file = Path(self.tmpdir) / "child.pid"
        script = (
            "sleep 120 &\n"
            "echo $! > '%s'\n"
            "sleep 120\n" % child_pid_file.as_posix()
        )
        code, _, _ = self.post_job(script, name="cancel-me")
        self.assertEqual(code, 200)
        deadline = time.time() + 5
        while time.time() < deadline and not child_pid_file.is_file():
            time.sleep(0.05)
        self.assertTrue(child_pid_file.is_file(), "child pid file missing")
        child_pid = int(child_pid_file.read_text().strip())
        self.assertTrue(pid_alive(child_pid))
        code, cur, _ = http("GET", self.base + "/jobs/current")
        self.assertEqual(code, 200)
        parent = int(cur["pid"])
        code, _, _ = http("POST", self.base + "/jobs/current/cancel")
        self.assertEqual(code, 200)
        deadline = time.time() + 5
        while time.time() < deadline and (pid_alive(parent) or pid_alive(child_pid)):
            time.sleep(0.05)
        self.assertFalse(pid_alive(parent), "session leader still alive")
        self.assertFalse(pid_alive(child_pid), "child still alive after cancel")

    def test_force_replaces_running_job(self):
        code, first, _ = self.post_job("sleep 30\n", name="old")
        self.assertEqual(code, 200)
        self.wait_status("running")
        code, second, _ = self.post_job("echo forced\n", name="new", force=True)
        self.assertEqual(code, 200)
        self.assertNotEqual(second["job_id"], first["job_id"])
        self.wait_log_contains("forced")

    def test_no_job_log_is_404(self):
        code, _, _ = http("GET", self.base + "/jobs/current/log?offset=0")
        self.assertEqual(code, 404)

    def test_cancel_with_no_live_job_is_404(self):
        code, _, _ = http("POST", self.base + "/jobs/current/cancel")
        self.assertEqual(code, 404)

    def test_does_not_write_machine_env(self):
        before = self.machine_env.read_text(encoding="utf-8")
        self.post_job("echo hi\n")
        self.wait_status("exited")
        self.assertEqual(self.machine_env.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main()
