"""M0 spike: drive a real runner on Windows and record how it behaves.

Run from a normal console window (not pythonw); needs RUNNERDECK_TEST_PAT / RUNNERDECK_TEST_REPO.

  install    download + SHA256 check + extract + config.cmd --unattended
             (--version 2.337.0 installs an older runner to capture a self-update)
  run        start run.cmd, tee stdout to spike-out/, log GitHub status changes; Ctrl+C stops
             (--dispatch-seconds N triggers the sandbox workflow once the runner is idle)
  stop-test  start, wait for idle (or a running job), stop with --method, then measure exit time,
             leftover processes, batch "Terminate (Y/N)" prompt and GitHub offline latency;
             appends one JSON line to spike-out/stop-results.jsonl
  procs      list Runner.* processes under --dir (orphan check), --kill to end them
  remove     config.cmd remove, falling back to DELETE /actions/runners/{id}

Stop methods:
  ctrl_break         CTRL_BREAK_EVENT to the process group, child shares our console
  ctrl_break_hidden  same, but child started with CREATE_NO_WINDOW (what a tray app would do)
  kill_listener      TerminateProcess on Runner.Listener.exe only
  taskkill           taskkill /PID <cmd.exe> /T /F
  taskkill_soft      taskkill /PID <cmd.exe> /T (no /F)
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import hashlib
import json
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import httpx

from spike_common import (
    SPIKE_OUT,
    Redactor,
    RunnerEvent,
    SpikeEnv,
    asset_sha256,
    check,
    classify_line,
    decode_output,
    extract_sha256,
    find_runner,
    find_runner_processes,
    get_release,
    load_env,
    make_client,
    pick_asset,
    read_runner_file,
    registration_token,
    remove_token,
    require_windows,
    stamp,
)

DEFAULT_DIR = r"D:\runners\spike"
DEFAULT_LABELS = "runnerdeck-spike"
WORKFLOW_FILE = "spike.yml"
CACHE_DIR = SPIKE_OUT / "cache"
STOP_METHODS = ("ctrl_break", "ctrl_break_hidden", "kill_listener", "taskkill", "taskkill_soft")
BATCH_PROMPTS = ("Terminate batch job", "终止批处理操作吗")


# --- runner process + output capture -----------------------------------------


class RunnerSession:
    """run.cmd child process with a reader thread that tees and classifies its output."""

    def __init__(self, runner_dir: Path, hidden_console: bool, redact: Redactor) -> None:
        self.runner_dir = runner_dir
        self.hidden_console = hidden_console
        self.redact = redact
        self.events: queue.Queue[tuple[float, RunnerEvent]] = queue.Queue()
        self.tail: collections.deque[str] = collections.deque(maxlen=2000)
        self.t0 = time.monotonic()
        self.proc: subprocess.Popen[bytes] | None = None
        self.log_base = SPIKE_OUT / f"run-{stamp()}"

    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    def start(self) -> None:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
        if self.hidden_console:
            flags |= subprocess.CREATE_NO_WINDOW
        SPIKE_OUT.mkdir(parents=True, exist_ok=True)
        self.t0 = time.monotonic()
        self.proc = subprocess.Popen(
            ["cmd.exe", "/c", "run.cmd"],
            cwd=self.runner_dir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        print(f"started run.cmd pid={self.proc.pid} hidden_console={self.hidden_console}")
        print(f"logging to {self.log_base}.log / .raw")
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc and self.proc.stdout
        with (
            open(f"{self.log_base}.raw", "ab") as raw_file,
            open(f"{self.log_base}.log", "a", encoding="utf-8") as text_file,
        ):
            for raw in iter(self.proc.stdout.readline, b""):
                t = self.elapsed()
                raw_file.write(raw)
                text = decode_output(raw).rstrip("\r\n")
                line = self.redact(f"[{t:8.2f}] {text}")
                text_file.write(line + "\n")
                text_file.flush()
                print(line, flush=True)
                self.tail.append(text)
                event = classify_line(text)
                if event:
                    self.events.put((t, event))

    def wait_event(self, kinds: set[str], timeout: float) -> tuple[float, RunnerEvent] | None:
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                t, event = self.events.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                if self.proc and self.proc.poll() is not None:
                    return None
                continue
            if event.kind in kinds:
                return t, event
        return None

    def wait_exit(self, timeout: float) -> float | None:
        assert self.proc
        try:
            self.proc.wait(timeout=timeout)
            return self.elapsed()
        except subprocess.TimeoutExpired:
            return None

    def saw_batch_prompt(self) -> bool:
        return any(p in line for line in self.tail for p in BATCH_PROMPTS)


class RemotePoller(threading.Thread):
    """Polls GitHub for the runner's status/busy and records every transition."""

    def __init__(self, env: SpikeEnv, name: str, session: RunnerSession, interval: float) -> None:
        super().__init__(daemon=True)
        self.env, self.name, self.session, self.interval = env, name, session, interval
        self.transitions: list[tuple[float, str, bool]] = []
        self._stop_flag = threading.Event()
        self._lock = threading.Lock()

    def run(self) -> None:
        last = None
        with make_client(self.env.pat) as client:
            while not self._stop_flag.is_set():
                try:
                    runner = find_runner(client, self.env, self.name)
                    state = (runner["status"], runner["busy"]) if runner else ("missing", False)
                    if state != last:
                        t = self.session.elapsed()
                        with self._lock:
                            self.transitions.append((t, *state))
                        print(
                            f"[{t:8.2f}] >>> GitHub: status={state[0]} busy={state[1]}", flush=True
                        )
                        last = state
                except Exception as e:  # keep polling through transient errors
                    print(f"[poller] {self.session.redact(str(e))}", flush=True)
                self._stop_flag.wait(self.interval)

    def stop(self) -> None:
        self._stop_flag.set()

    def wait_for(self, status: str, since: float, timeout: float) -> float | None:
        """Earliest time >= since at which GitHub's status matched; '!online' means not online.

        Returns `since` itself when the state in effect at `since` already matched.
        """
        negate = status.startswith("!")
        target = status.lstrip("!")

        def matches(s: str) -> bool:
            return (s != target) if negate else (s == target)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                before = [tr for tr in self.transitions if tr[0] <= since]
                if before and matches(before[-1][1]):
                    return since
                for t, s, _busy in self.transitions:
                    if t > since and matches(s):
                        return t
            time.sleep(0.5)
        return None


def apply_stop(method: str, session: RunnerSession) -> str:
    assert session.proc
    pid = session.proc.pid
    if method.startswith("ctrl_break"):
        try:
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        except OSError as e:  # e.g. child has no console shared with us
            return f"CTRL_BREAK_EVENT -> group {pid} failed: {e}"
        return f"CTRL_BREAK_EVENT -> group {pid}"
    if method == "kill_listener":
        listeners = [
            p
            for p in find_runner_processes(session.runner_dir)
            if (p.info.get("name") or "").lower() == "runner.listener.exe"
        ]
        for p in listeners:
            p.kill()
        return f"killed Runner.Listener.exe pids={[p.pid for p in listeners]}"
    args = ["taskkill", "/PID", str(pid), "/T"] + (["/F"] if method == "taskkill" else [])
    out = subprocess.run(args, capture_output=True)
    return (
        f"{' '.join(args)} -> rc={out.returncode} {decode_output(out.stdout + out.stderr).strip()}"
    )


def force_cleanup(session: RunnerSession) -> list[int]:
    killed = []
    if session.proc and session.proc.poll() is None:
        subprocess.run(["taskkill", "/PID", str(session.proc.pid), "/T", "/F"], capture_output=True)
    for p in find_runner_processes(session.runner_dir):
        with contextlib.suppress(Exception):
            p.kill()
            killed.append(p.pid)
    return killed


def dispatch_job(client: httpx.Client, env: SpikeEnv, seconds: int, fail: bool) -> bool:
    repo = check(client.get(f"/repos/{env.full_name}")).json()
    resp = client.post(
        f"/repos/{env.full_name}/actions/workflows/{WORKFLOW_FILE}/dispatches",
        json={
            "ref": repo["default_branch"],
            "inputs": {"seconds": str(seconds), "fail": str(fail).lower()},
        },
    )
    if resp.status_code == 204:
        print(f">>> dispatched {WORKFLOW_FILE} (seconds={seconds}, fail={fail})")
        return True
    print(
        f">>> dispatch failed ({resp.status_code}); trigger it yourself:\n"
        f"    gh workflow run {WORKFLOW_FILE} -R {env.full_name} -f seconds={seconds}"
    )
    return False


def latest_dispatch_run(client: httpx.Client, env: SpikeEnv) -> dict | None:
    resp = check(
        client.get(
            f"/repos/{env.full_name}/actions/runs",
            params={"per_page": 1, "event": "workflow_dispatch"},
        )
    )
    runs = resp.json()["workflow_runs"]
    return runs[0] if runs else None


def runner_name(runner_dir: Path) -> str:
    path = runner_dir / ".runner"
    if not path.exists():
        sys.exit(f"{path} not found; run `install` first")
    return read_runner_file(path)["agentName"]


# --- commands ------------------------------------------------------------------


def download(url: str, dest: Path, expected_sha: str | None) -> Path:
    def sha_of(path: Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    if dest.exists() and expected_sha and sha_of(dest) == expected_sha:
        print(f"cache hit {dest.name}")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    h = hashlib.sha256()
    t0 = time.monotonic()
    # Separate client: never send the PAT to the release CDN.
    with httpx.Client(follow_redirects=True, timeout=60) as cdn, cdn.stream("GET", url) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        done, next_mark = 0, 0.1
        with open(part, "wb") as f:
            for chunk in resp.iter_bytes(1 << 20):
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if total and done / total >= next_mark:
                    print(f"  {done / total:4.0%} {done / 1e6:.0f} MB")
                    next_mark += 0.1
    actual = h.hexdigest()
    print(f"downloaded {done / 1e6:.1f} MB in {time.monotonic() - t0:.1f}s sha256={actual}")
    if expected_sha and actual != expected_sha:
        part.unlink()
        sys.exit(f"SHA256 mismatch: expected {expected_sha}")
    os.replace(part, dest)
    return dest


def cmd_install(args: argparse.Namespace) -> int:
    require_windows()
    env = load_env()
    redact = Redactor()
    redact.add(env.pat)
    target = Path(args.dir)
    if target.exists() and any(target.iterdir()):
        sys.exit(f"{target} is not empty; use `remove --delete-dir` or another --dir")
    with make_client(env.pat) as client:
        rel = get_release(client, args.version)
        asset = pick_asset(rel)
        expected = asset_sha256(asset) or extract_sha256(rel.get("body") or "")
        print(f"release {rel['tag_name']} {asset['name']} expected sha256={expected}")
        zip_path = download(asset["browser_download_url"], CACHE_DIR / asset["name"], expected)

        t0 = time.monotonic()
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(target)
        print(f"extracted to {target} in {time.monotonic() - t0:.1f}s")

        token = registration_token(client, env)["token"]
        redact.add(token)
        cmd = [
            "cmd.exe", "/c", "config.cmd", "--unattended",
            "--url", f"https://github.com/{env.full_name}",
            "--token", token,
            "--name", args.name,
            "--labels", args.labels,
            "--work", "_work",
            "--replace",
        ]  # fmt: skip
        t0 = time.monotonic()
        proc = subprocess.run(
            cmd, cwd=target, stdin=subprocess.DEVNULL, capture_output=True, timeout=120
        )
        output = redact(decode_output(proc.stdout) + decode_output(proc.stderr))
        SPIKE_OUT.mkdir(parents=True, exist_ok=True)
        log = SPIKE_OUT / f"config-{stamp()}.log"
        log.write_text(output, encoding="utf-8")
        print(output)
        print(f"config.cmd rc={proc.returncode} in {time.monotonic() - t0:.1f}s, log {log}")
        if proc.returncode != 0:
            return proc.returncode

        info = read_runner_file(target / ".runner")
        print(f".runner keys={sorted(info)}")
        print(f"agentId={info.get('agentId')} agentName={info.get('agentName')}")
        remote = find_runner(client, env, args.name)
        print(f"GitHub sees: {remote and {k: remote[k] for k in ('id', 'status', 'busy')}}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    require_windows()
    env = load_env()
    redact = Redactor()
    redact.add(env.pat)
    runner_dir = Path(args.dir)
    session = RunnerSession(runner_dir, hidden_console=args.console == "hidden", redact=redact)
    poller = RemotePoller(env, runner_name(runner_dir), session, args.poll)
    session.start()
    poller.start()
    try:
        if args.dispatch_seconds and session.wait_event({"idle"}, 300):
            with make_client(env.pat) as client:
                dispatch_job(client, env, args.dispatch_seconds, args.dispatch_fail)
        deadline = time.monotonic() + args.duration if args.duration else None
        while session.proc and session.proc.poll() is None:
            if deadline and time.monotonic() > deadline:
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nCtrl+C: stopping runner")
    if session.proc and session.proc.poll() is None:
        method = "ctrl_break" if args.console == "shared" else "taskkill"
        print(apply_stop(method, session))
        if session.wait_exit(15) is None:
            print(f"still running, force cleanup killed {force_cleanup(session)}")
    time.sleep(args.poll * 2)  # let the poller see the offline transition
    poller.stop()
    return 0


def cmd_stop_test(args: argparse.Namespace) -> int:
    require_windows()
    env = load_env()
    redact = Redactor()
    redact.add(env.pat)
    runner_dir = Path(args.dir)
    name = runner_name(runner_dir)
    if args.method == "ctrl_break":
        console = "shared"
    elif args.method == "ctrl_break_hidden":
        console = "hidden"
    else:
        console = args.console or "hidden"  # what the tray app will use
    result: dict = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "method": args.method,
        "console": console,
        "during_job": args.during_job,
    }
    session = RunnerSession(runner_dir, hidden_console=console == "hidden", redact=redact)
    poller = RemotePoller(env, name, session, args.poll)
    session.start()
    poller.start()
    try:
        measure_stop(args, env, session, poller, result)
    finally:
        cleaned = force_cleanup(session)
        if cleaned:
            result["cleanup_at_exit"] = cleaned
    return finish(result, session, poller)


def measure_stop(
    args: argparse.Namespace,
    env: SpikeEnv,
    session: RunnerSession,
    poller: RemotePoller,
    result: dict,
) -> None:
    redact = session.redact
    runner_dir = session.runner_dir
    idle = session.wait_event({"idle"}, args.idle_timeout)
    if not idle:
        result["error"] = "never reached idle"
        return
    result["t_idle"] = round(idle[0], 2)
    result["t_online"] = poller.wait_for("online", 0, 60)

    if args.during_job:
        with make_client(env.pat) as client:
            dispatch_job(client, env, args.job_seconds, fail=False)
        busy = session.wait_event({"busy"}, args.busy_timeout)
        if not busy:
            result["error"] = "job never started"
            return
        result["t_busy"], result["job"] = round(busy[0], 2), busy[1].detail
        time.sleep(args.job_settle)

    t_stop = session.elapsed()
    result["t_stop"] = round(t_stop, 2)
    result["action"] = redact(apply_stop(args.method, session))
    t_exit = session.wait_exit(args.exit_timeout)
    result["exit_after_s"] = None if t_exit is None else round(t_exit - t_stop, 2)
    result["returncode"] = session.proc.returncode if session.proc else None
    time.sleep(2)  # give orphaned children a moment to show up
    leftover = find_runner_processes(runner_dir)
    result["leftover"] = [{"pid": p.pid, "name": p.info.get("name")} for p in leftover]
    result["batch_prompt_seen"] = session.saw_batch_prompt()
    if t_exit is None or leftover:
        # Leftovers would keep the runner online forever; clean up, then measure from there.
        result["force_cleaned"] = force_cleanup(session)
        result["offline_measured_after_cleanup"] = True

    t_offline = poller.wait_for("!online", t_stop, args.offline_timeout)
    result["offline_after_s"] = None if t_offline is None else round(t_offline - t_stop, 2)

    if args.during_job:
        with make_client(env.pat) as client:
            run = latest_dispatch_run(client, env)
            deadline = time.monotonic() + args.wait_conclusion
            while run and run["status"] != "completed" and time.monotonic() < deadline:
                time.sleep(10)
                run = latest_dispatch_run(client, env)
            if run:
                result["run"] = {
                    "url": run["html_url"],
                    "status": run["status"],
                    "conclusion": run["conclusion"],
                }


def finish(result: dict, session: RunnerSession, poller: RemotePoller) -> int:
    if session.proc and session.proc.poll() is None:
        force_cleanup(session)
    poller.stop()
    result["github_transitions"] = poller.transitions
    result["log"] = str(session.log_base) + ".log"
    SPIKE_OUT.mkdir(parents=True, exist_ok=True)
    with open(SPIKE_OUT / "stop-results.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")
    print("\n=== result ===")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 1 if "error" in result else 0


def cmd_procs(args: argparse.Namespace) -> int:
    require_windows()
    procs = find_runner_processes(Path(args.dir))
    for p in procs:
        with contextlib.suppress(Exception):
            print(f"{p.pid:>7} ppid={p.ppid():>7} {p.name():<24} {p.exe()}")
    if not procs:
        print("no Runner.* processes under", args.dir)
    if args.kill:
        for p in procs:
            with contextlib.suppress(Exception):
                p.kill()
        print(f"killed {[p.pid for p in procs]}")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    require_windows()
    env = load_env()
    redact = Redactor()
    redact.add(env.pat)
    runner_dir = Path(args.dir)
    if find_runner_processes(runner_dir):
        sys.exit("runner processes still alive under --dir; stop them first (`procs --kill`)")
    runner_file = runner_dir / ".runner"
    info = read_runner_file(runner_file) if runner_file.exists() else None
    removed = False
    with make_client(env.pat) as client:
        if info and not args.api_only:
            token = remove_token(client, env)["token"]
            redact.add(token)
            proc = subprocess.run(
                ["cmd.exe", "/c", "config.cmd", "remove", "--token", token],
                cwd=runner_dir,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=120,
            )
            output = redact(decode_output(proc.stdout) + decode_output(proc.stderr))
            SPIKE_OUT.mkdir(parents=True, exist_ok=True)
            (SPIKE_OUT / f"remove-{stamp()}.log").write_text(output, encoding="utf-8")
            print(output)
            print(f"config.cmd remove rc={proc.returncode}")
            removed = proc.returncode == 0
        if not removed:
            agent_id = info.get("agentId") if info else None
            if agent_id is None:
                remote = find_runner(client, env, args.name) if args.name else None
                agent_id = remote and remote["id"]
            if agent_id is None:
                sys.exit("no .runner and no --name match on GitHub; nothing to remove")
            resp = client.delete(f"/repos/{env.full_name}/actions/runners/{agent_id}")
            print(f"DELETE runners/{agent_id} -> {resp.status_code}")
            removed = resp.status_code == 204
    if args.delete_dir and runner_dir.exists():
        if not (runner_dir / "config.cmd").exists():
            sys.exit(f"{runner_dir} has no config.cmd; refusing to delete it")
        shutil.rmtree(runner_dir)
        print(f"deleted {runner_dir}")
    return 0 if removed else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, **kw: object) -> argparse.ArgumentParser:
        p = sub.add_parser(name, **kw)
        p.add_argument(
            "--dir", default=DEFAULT_DIR, help=f"runner directory (default {DEFAULT_DIR})"
        )
        return p

    repo = os.environ.get("RUNNERDECK_TEST_REPO", "sandbox").rsplit("/", 1)[-1]
    p = add("install")
    p.add_argument("--name", default=f"{socket.gethostname()}-{repo}")
    p.add_argument("--labels", default=DEFAULT_LABELS)
    p.add_argument("--version", help="runner version without v, default latest")
    p.set_defaults(func=cmd_install)

    p = add("run")
    p.add_argument("--console", choices=("shared", "hidden"), default="shared")
    p.add_argument("--duration", type=float, default=0, help="auto stop after N seconds")
    p.add_argument("--poll", type=float, default=5)
    p.add_argument("--dispatch-seconds", type=int, default=0)
    p.add_argument("--dispatch-fail", action="store_true")
    p.set_defaults(func=cmd_run)

    p = add("stop-test")
    p.add_argument("--method", choices=STOP_METHODS, required=True)
    p.add_argument("--console", choices=("shared", "hidden"))
    p.add_argument("--during-job", action="store_true")
    p.add_argument("--job-seconds", type=int, default=180)
    p.add_argument("--job-settle", type=float, default=10)
    p.add_argument("--poll", type=float, default=2)
    p.add_argument("--idle-timeout", type=float, default=300)
    p.add_argument("--busy-timeout", type=float, default=300)
    p.add_argument("--exit-timeout", type=float, default=15)
    p.add_argument("--offline-timeout", type=float, default=300)
    p.add_argument("--wait-conclusion", type=float, default=0)
    p.set_defaults(func=cmd_stop_test)

    p = add("procs")
    p.add_argument("--kill", action="store_true")
    p.set_defaults(func=cmd_procs)

    p = add("remove")
    p.add_argument("--name", help="runner name, used only when .runner is gone")
    p.add_argument("--api-only", action="store_true", help="skip config.cmd, test the DELETE path")
    p.add_argument("--delete-dir", action="store_true")
    p.set_defaults(func=cmd_remove)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
