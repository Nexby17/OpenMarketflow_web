"""F-229: local robot supervisor — lets the UI start robots that the C# server does not know.

Why this exists
---------------
The UI buttons call POST /api/of/process/{name}/start on the C# server. That endpoint needs a UI
session (401 when the cookie expires) and only knows the robots hard-coded in RobotProcessManager.
There is no .NET SDK on this machine (runtime only), so a new instance such as `of_br_edp2` (:5086)
cannot be added there. Result: pressing ▶ on such a robot could never resurrect a dead process.

This service is a tiny local spawner with an explicit whitelist. It only knows the robots listed in
ROBOTS below — it cannot run arbitrary commands from the network.

API (JSON, CORS *)
    GET  /health                 -> {ok, service, robots:[names]}
    GET  /list                   -> per robot: listening port, pid
    POST /start?name=NAME        -> spawn if the port is free; idempotent if already listening
    POST /stop?name=NAME&force=1 -> kill the process listening on that port (process only, NOT trading)
    POST /selftest               -> spawn a dummy python process, proves the spawn machinery

Safety
------
* binds 0.0.0.0 so the UI works both from localhost and from the LAN (robots themselves bind 0.0.0.0),
  but requests from non-private addresses are rejected; every request is logged with the client IP;
* only whitelisted names are accepted — no shell, no user-supplied paths;
* /stop requires force=1 and only terminates a process, it never sends trading commands.

Run:  python scripts/robot_supervisor.py [--port 5099]
"""

import argparse
import datetime as _dt
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
ROBOT_DIR = os.path.join(REPO, "robot")
LOGS_DIR = os.path.join(ROBOT_DIR, "logs")
ENV_FILE = os.path.join(REPO, "src", "Server", "bin", "Debug", "net10.0", ".env")

PYTHON = os.environ.get("PYTHON3_EXE") or r"D:\AutoClaw\resources\python\python3.exe"
if not os.path.exists(PYTHON):
    PYTHON = sys.executable

# name -> (script, port, {env overrides})
ROBOTS = {
    "of":         ("main_of.py",      5080, {}),
    "of_mx":      ("main_of_mx.py",   5081, {}),
    "of_br":      ("main_of_br.py",   5082, {"ROBOT_STATE_SUFFIX_BR": "", "ROBOT_ACTIVE_ACCOUNT_BR": ""}),
    "of_br_edp":  ("main_of_br.py",   5083, {"ROBOT_STATE_SUFFIX_BR": "_edp", "ROBOT_ACTIVE_ACCOUNT_BR": "edp"}),
    # F-219/F-220: second BR instance, January Brent. Kept in sync with app.js OF_BR_INSTANCES.edp2
    "of_br_edp2": ("main_of_br.py",   5086, {"ROBOT_STATE_SUFFIX_BR": "_edp2", "ROBOT_ACTIVE_ACCOUNT_BR": "edp",
                                             "ROBOT_SYMBOL_BR": "BRF7@RTSX", "ROBOT_TICKER_BR": "BRF7"}),
    "of_perp":    ("main_of_perp.py", 5095, {}),
}

_LOG_LOCK = threading.Lock()


def _log(msg: str) -> None:
    line = "%s %s" % (_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        with _LOG_LOCK:
            with open(os.path.join(LOGS_DIR, "supervisor_%s.log" % _dt.date.today().strftime("%Y%m%d")),
                      "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass
    try:
        print(line, flush=True)
    except Exception:
        pass


def _env_from_file() -> dict:
    """FINAM key etc. from the server .env (same file the C# server uses)."""
    out = {}
    try:
        with open(ENV_FILE, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    except Exception as e:
        _log("WARN cannot read .env: %s" % e)
    return out


def _pid_on_port(port: int):
    """PID of the process LISTENING on the port (Windows netstat, POSIX lsof fallback).

    F-229b: netstat on a localized Windows writes the table in the OEM codepage (cp866),
    so text=True/utf-8 killed the reader thread and pids silently came back as None.
    Read bytes and decode leniently — we only match the ASCII columns.
    """
    try:
        if os.name == "nt":
            raw = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, timeout=15).stdout or b""
            for enc in ("utf-8", "cp866", "cp1251", "latin-1"):
                try:
                    out = raw.decode(enc)
                    break
                except Exception:
                    out = raw.decode("utf-8", "ignore")
            for line in out.splitlines():
                parts = line.split()
                # columns: Proto  Local  Foreign  State  PID   (State is English: LISTENING)
                if len(parts) >= 5 and parts[3].upper().startswith("LISTEN") and parts[1].endswith(":%d" % port):
                    try:
                        return int(parts[4])
                    except ValueError:
                        return None
        else:
            out = subprocess.run(["lsof", "-ti", "tcp:%d" % port, "-sTCP:LISTEN"], capture_output=True,
                                 text=True, timeout=10).stdout.strip()
            if out:
                return int(out.splitlines()[0])
    except Exception as e:
        _log("WARN pid lookup %s: %s" % (port, e))
    return None


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _spawn(name: str):
    script, port, overrides = ROBOTS[name]
    if _port_open(port):
        pid = _pid_on_port(port)
        return {"ok": True, "alreadyRunning": True, "pid": pid, "port": port}

    env = os.environ.copy()
    env.update(_env_from_file())
    env.update({k: v for k, v in overrides.items() if v is not None})
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    os.makedirs(LOGS_DIR, exist_ok=True)
    stamp = _dt.date.today().strftime("%Y%m%d")
    out_path = os.path.join(LOGS_DIR, "%s_supervisor_%s.log" % (name, stamp))
    err_path = os.path.join(LOGS_DIR, "%s_supervisor_%s.err" % (name, stamp))

    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

    with open(out_path, "a", encoding="utf-8") as out, open(err_path, "a", encoding="utf-8") as err:
        proc = subprocess.Popen(
            [PYTHON, script, "--port", str(port)],
            cwd=ROBOT_DIR, env=env, stdout=out, stderr=err,
            creationflags=flags,
            start_new_session=(os.name != "nt"),
        )
    _log("SPAWN %s -> pid=%s port=%s script=%s" % (name, proc.pid, port, script))

    deadline = time.time() + 30
    while time.time() < deadline:
        if _port_open(port):
            return {"ok": True, "pid": proc.pid, "port": port, "started": True}
        if proc.poll() is not None:
            return {"ok": False, "error": "процесс завершился при старте (rc=%s), см. %s" %
                                          (proc.returncode, os.path.basename(err_path))}
        time.sleep(0.7)
    return {"ok": False, "error": "порт %d не поднялся за 30с (pid=%s)" % (port, proc.pid)}


def _selftest():
    code = "import sys; print('supervisor selftest ok'); sys.exit(0)"
    env = os.environ.copy()
    r = subprocess.run([PYTHON, "-c", code], capture_output=True, text=True, timeout=20, env=env)
    _log("SELFTEST rc=%s out=%r" % (r.returncode, (r.stdout or "").strip()[:120]))
    return {"ok": r.returncode == 0, "rc": r.returncode, "stdout": (r.stdout or "").strip()[:200],
            "python": PYTHON}


def _is_private(host: str) -> bool:
    if host in ("127.0.0.1", "::1", "localhost"):
        return True
    ip = host.rsplit(".", 1)[0]
    return host.startswith("10.") or host.startswith("192.168.") or ip.startswith("172.16.") or \
        any(host.startswith("172.%d." % n) for n in range(16, 32))


class Handler(BaseHTTPRequestHandler):
    server_version = "RobotSupervisor/1.0"

    def log_message(self, fmt, *args):  # silence default stderr logging
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send({"ok": True})

    def do_GET(self):
        if not _is_private(self.client_address[0]):
            _log("REJECT GET from %s" % self.client_address[0])
            return self._send({"ok": False, "error": "forbidden"}, 403)
        u = urlparse(self.path)
        if u.path == "/health":
            return self._send({"ok": True, "service": "robot_supervisor", "robots": sorted(ROBOTS)})
        if u.path == "/list":
            data = {}
            for name, (_s, port, _e) in ROBOTS.items():
                data[name] = {"port": port, "listening": _port_open(port), "pid": _pid_on_port(port)}
            return self._send({"ok": True, "robots": data})
        return self._send({"ok": False, "error": "unknown path"}, 404)

    def do_POST(self):
        ip = self.client_address[0]
        if not _is_private(ip):
            _log("REJECT POST from %s" % ip)
            return self._send({"ok": False, "error": "forbidden"}, 403)
        u = urlparse(self.path)
        q = parse_qs(u.query)
        name = (q.get("name") or [""])[0]

        if u.path == "/selftest":
            return self._send(_selftest())

        if u.path not in ("/start", "/stop"):
            return self._send({"ok": False, "error": "unknown path"}, 404)
        if name not in ROBOTS:
            _log("DENY %s unknown robot=%r from %s" % (u.path, name, ip))
            return self._send({"ok": False, "error": "unknown robot: %s (allowed: %s)" %
                                                     (name, ", ".join(sorted(ROBOTS)))}, 400)

        _log("%s name=%s from %s" % (u.path.upper().lstrip("/"), name, ip))

        if u.path == "/start":
            try:
                return self._send(_spawn(name))
            except Exception as e:
                _log("ERROR start %s: %s" % (name, e))
                return self._send({"ok": False, "error": str(e)}, 500)

        # /stop — process only, never trading
        if (q.get("force") or [""])[0] != "1":
            return self._send({"ok": False, "error": "нужен force=1 (это убивает процесс, не позицию)"}, 400)
        port = ROBOTS[name][1]
        pid = _pid_on_port(port)
        if not pid:
            return self._send({"ok": True, "wasNotRunning": True})
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=20)
            else:
                os.kill(pid, 9)
            _log("KILL %s pid=%s" % (name, pid))
            return self._send({"ok": True, "killed": pid})
        except Exception as e:
            return self._send({"ok": False, "error": str(e)}, 500)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5099)
    ap.add_argument("--bind", default="0.0.0.0")
    args = ap.parse_args()
    _log("supervisor starting on %s:%d python=%s robots=%s" % (args.bind, args.port, PYTHON, ",".join(sorted(ROBOTS))))
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
