#!/usr/bin/env python3
"""Reach a machine that takes no ssh logins: owner-side RPC over OpenSSH Unix-socket forwarding.

Each owner machine (one that runs agents) keeps an outbound ssh tunnel to a hub, which needs nothing but
sshd. A request to an owner goes through the hub to that owner's socket and back. Every request and answer
is signed with relay.key, which only the owner machines hold, so the hub (or anything else on it) can
neither read commands into an owner nor forge an answer.

Config (peer's config folder): relay.json, relay.key, and hosts.json entries {"relay": "<hub name>"}."""
import concurrent.futures
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shlex
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time

import peer_core

CONFIG = peer_core.config()
RUN = peer_core.root() / "relay"
PEER = Path(__file__).resolve().with_name("peer")
MAX_FRAME = 8 * 1024 * 1024
# How long one remote call may wait. A caller that sweeps many machines at once (a monitor) lowers it with
# PEER_RELAY_TIMEOUT, so one sleeping machine cannot use up its whole budget.
TIMEOUT = int(os.environ.get("PEER_RELAY_TIMEOUT", "25"))
LOG_LIMIT = 2 * 1024 * 1024
LOG_KEEP = 256 * 1024
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
# Shared by the owner machines only. Every process of a socket's account can open,
# replace or impersonate it, so the socket path proves nothing about who is calling;
# the key does. Keep it off the hub and off any account that should not drive your agents.
KEY = CONFIG / "relay.key"
SKEW = 60
LABEL = "io.github.oesoll.peer-relay"  # launchd job; systemd uses peer-relay.service
NONCE = re.compile(r"[0-9a-f]{32}\Z")


def name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError("invalid host/relay name")
    return value


def receive(stream):
    line = stream.readline(MAX_FRAME + 1)
    if not line.endswith(b"\n") or len(line) > MAX_FRAME:
        raise ValueError("missing or oversized RPC frame")
    return json.loads(line)


def send(stream, value):
    data = json.dumps(value, ensure_ascii=True).encode() + b"\n"
    if len(data) > MAX_FRAME:
        raise ValueError("RPC output exceeds 8 MiB; request fewer capture lines")
    stream.write(data)
    stream.flush()


def secret():
    try:
        info = KEY.stat()
        text = KEY.read_text().strip()
    except FileNotFoundError:
        raise ValueError("relay key missing: run `peer relay keygen` on one owner machine and copy "
                         + str(KEY) + " (mode 600) to the others, never to the hub")
    if info.st_mode & 0o077:
        raise ValueError(str(KEY) + " must be readable by its owner only (chmod 600)")
    try:
        value = bytes.fromhex(text)
    except ValueError:
        value = b""
    if len(value) < 32:
        raise ValueError(str(KEY) + " must hold at least 32 hex-encoded random bytes")
    return value


def sign(key, *parts):
    data = json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode()
    return hmac.new(key, data, hashlib.sha256).hexdigest()


def endpoint(relay, owner):
    """The socket that reaches owner through hub relay: this machine's forward of it, or, on the hub itself,
    the end of owner's own tunnel."""
    path = RUN / (name(relay) + "-" + name(owner) + ".sock")
    if not path.exists() and (RUN / relay / (owner + ".sock")).exists():
        path = RUN / relay / (owner + ".sock")
    return path


def call(relay, owner, args):
    return request(endpoint(relay, owner), owner, args)


def request(path, target, args, key=None):
    key = secret() if key is None else key
    stamp, nonce = int(time.time()), secrets.token_hex(16)
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(TIMEOUT + 5)
        sock.connect(str(path))
        with sock.makefile("rwb") as stream:
            send(stream, {"version": 1, "to": target, "ts": stamp, "nonce": nonce, "args": args,
                          "mac": sign(key, "request", target, stamp, nonce, args)})
            result = receive(stream)
    if not isinstance(result, dict) or not isinstance(result.get("code"), int):
        raise ValueError("invalid RPC response")
    out, err = result.get("out", ""), result.get("err", "")
    if not (isinstance(out, str) and isinstance(err, str) and isinstance(result.get("mac"), str)
            and hmac.compare_digest(result["mac"], sign(key, "response", nonce, result["code"], out, err))):
        # Whatever answered is not an owner machine (or holds a different key);
        # its text could be forged pane content, so show only a bounded hint.
        raise ValueError("relay response failed authentication (relay.key differs, or the hub socket "
                         "is not the peer); unverified: " + err.strip()[:160])
    return result


class Replay:
    """Rejects stale or repeated requests: the hub can record and resend them."""

    def __init__(self, clock=time.time):
        self.clock, self.seen, self.lock = clock, {}, threading.Lock()

    def check(self, stamp, nonce):
        now = self.clock()
        if abs(now - stamp) > SKEW:
            raise ValueError("request timestamp outside the " + str(SKEW) + "s window (check clocks)")
        with self.lock:
            self.seen = {k: t for k, t in self.seen.items() if now - t <= 2 * SKEW}
            if nonce in self.seen:
                raise ValueError("replayed request")
            if len(self.seen) >= 100_000:
                raise ValueError("too many requests in the replay window")
            self.seen[nonce] = now


def authenticate(value, local, key, replay):
    if not isinstance(value, dict):
        raise ValueError("invalid RPC request")
    stamp, nonce, mac = value.get("ts"), value.get("nonce"), value.get("mac")
    if (value.get("to") != local or not isinstance(stamp, int) or not isinstance(nonce, str)
            or not NONCE.fullmatch(nonce) or not isinstance(mac, str)):
        raise ValueError("unauthenticated request refused")
    if not hmac.compare_digest(mac, sign(key, "request", local, stamp, nonce, value.get("args"))):
        raise ValueError("unauthenticated request refused")
    replay.check(stamp, nonce)
    return nonce


def execute(value):
    args = value.get("args") if isinstance(value, dict) else None
    if (not isinstance(value, dict) or value.get("version") != 1 or not isinstance(args, list)
            or not args or len(args) > 128 or not all(isinstance(x, str) and "\0" not in x for x in args)):
        raise ValueError("invalid relay request")
    if len(args) == 3 and args[0] == '_rpc':
        name(args[1])
        # Send the body on stdin: large Unicode messages must not hit the OS argv limit.
        command, data = [sys.executable, str(PEER), '_rpc', args[1]], args[2].encode()
    else:
        raise ValueError("only peer _rpc requests are accepted")
    env = dict(os.environ)
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    result = subprocess.run(command, input=data, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=TIMEOUT)
    return {"code": result.returncode, "out": result.stdout.decode(errors="replace"),
            "err": result.stderr.decode(errors="replace")}


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(TIMEOUT + 5)
        server, nonce = self.server, ""
        try:
            value = receive(self.rfile)
            nonce = authenticate(value, server.local, server.key, server.replay)
            result = execute(value)
        except (ValueError, OSError, subprocess.SubprocessError) as error:
            result = {"code": 1, "out": "", "err": "error: " + str(error) + "\n"}
        # Signed with the request's nonce so a caller can tell this answer from
        # one the hub made up. Refusals carry the nonce too, or "" when none was valid.
        result["mac"] = sign(server.key, "response", nonce, result["code"], result["out"], result["err"])
        try:
            send(self.wfile, result)
        except (ValueError, OSError):
            pass


class Server(socketserver.UnixStreamServer):
    # Small bounded pool: each request is one short CLI invocation. If sustained
    # contention appears, add per-target scheduling; never queue unlimited work.
    def __init__(self, path, local, key):
        self.local, self.key, self.replay = local, key, Replay()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=16)
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(str(path), Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        self.pool.submit(self.finish, request, address)

    def finish(self, request, address):
        try:
            self.finish_request(request, address)
        finally:
            self.shutdown_request(request)
            self.slots.release()

    def server_close(self):
        super().server_close()
        self.pool.shutdown(wait=True)


def hubs():
    """Every hub this owner keeps an outbound tunnel to.

    relay.json is either one hub ({"name", "ssh_alias", "remote_dir"}) or {"hubs": [...]}.
    An owner with no inbound sshd (a laptop with Remote Login off) tunnels into each peer directly, so no
    third machine carries the traffic. A hub peer that is itself the far end of a tunnel
    reaches this owner through RUN/<hub>/<owner>.sock (see endpoint)."""
    from peer_transport import hosts
    local = name(peer_core.host())
    try:
        conf = json.loads((CONFIG / "relay.json").read_text())
    except FileNotFoundError:
        raise ValueError("no relay configured: write " + str(CONFIG / "relay.json")
                         + ' as {"name": "<hub>", "ssh_alias": "<ssh host>", "remote_dir": "<absolute folder on the hub>"}')
    entries = conf["hubs"] if isinstance(conf, dict) and "hubs" in conf else [conf]
    if not isinstance(entries, list) or not entries:
        raise ValueError("relay.json needs at least one hub")
    routes = [(name(host), name(entry["relay"])) for host, entry in hosts().items() if entry.get("relay")]
    result, seen = [], set()
    for entry in entries:
        relay, alias, root = name(entry["name"]), name(entry["ssh_alias"]), entry["remote_dir"]
        if relay in seen:
            raise ValueError("duplicate hub name in relay.json: " + relay)
        seen.add(relay)
        if not isinstance(root, str) or not root.startswith("/") or ":" in root or "\n" in root:
            raise ValueError("relay remote_dir must be an absolute path without colons/newlines")
        result.append((local, relay, alias, root.rstrip("/"), [h for h, r in routes if r == relay]))
    if len(result) == 1:
        # One hub: every @ route must name it (unchanged single-relay contract).
        for host, route in routes:
            if route != result[0][1]:
                raise ValueError("one relay per daemon is supported; all @ routes must match relay.json")
    return result


def configuration():
    """The first hub, for single-relay callers and tests."""
    return hubs()[0]


def ssh_options():
    # A tunnel owns its connection. Do not attach its lifetime to an unrelated
    # multiplexed command; short direct-ssh calls may still use masters.
    return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
            "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
            "-o", "ControlMaster=no", "-o", "ControlPath=none"]


def tunnel_command(local, relay, alias, root, peers):
    args = ssh_options() + ["-o", "ExitOnForwardFailure=yes", "-o", "StreamLocalBindUnlink=yes",
                            "-R", root + "/" + local + ".sock:" + str(RUN / "receiver.sock")]
    for peer in peers:
        args += ["-L", str(RUN / (relay + "-" + peer + ".sock")) + ":" + root + "/" + peer + ".sock"]
    return args + ["-N", alias]


def prepare_hub(local, alias, root):
    # Only unlink this daemon's generated socket, never arbitrary files. Host
    # names must be unique on the hub. Local flock prevents duplicate daemons;
    # reconnect replaces a stale remote listener left by a lost SSH connection.
    path = root + "/" + local + ".sock"
    script = ('umask 077; mkdir -p -- {root} && chmod 700 -- {root} && '
              '(test ! -e {path} || test -S {path}) && rm -f -- {path}').format(
                  root=shlex.quote(root), path=shlex.quote(path))
    subprocess.run(ssh_options() + [alias, script], check=True, timeout=15, stderr=subprocess.PIPE)


def log(message):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + message, file=sys.stderr, flush=True)


def reconnect_delay(failures):
    """3s, then doubling per consecutive failure up to 60s.

    Away from home or asleep the hub is unreachable for hours; a fixed 3s retry
    wrote ~1,200 lines an hour and left 2,300 undated failures in the log.
    """
    return min(60, 3 * 2 ** max(0, failures - 1))


def failure_summary(error):
    if isinstance(error, subprocess.CalledProcessError):
        detail = (error.stderr or b"").decode(errors="replace").strip().splitlines()
        return "hub ssh exited " + str(error.returncode) + (": " + detail[-1] if detail else "")
    if isinstance(error, subprocess.TimeoutExpired):
        return "hub ssh timed out"
    return str(error)


def trim_log(path, fd=2, limit=LOG_LIMIT, keep=LOG_KEEP):
    """Bound the launchd log in place. Only when our stderr *is* that file —
    systemd sends stderr to journald, which rotates on its own. Truncating
    through the shared descriptor works whether or not launchd opened it O_APPEND."""
    try:
        info = os.fstat(fd)
        if info.st_size <= limit or os.stat(path).st_ino != info.st_ino:
            return False
        with open(path, "rb") as stream:
            stream.seek(-keep, os.SEEK_END)
            tail = stream.read()
        tail = tail[tail.find(b"\n") + 1:]
        sys.stderr.flush()
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, tail)
        return True
    except OSError:
        return False


def pump_stderr(stream):
    # ssh's own diagnostics ("Timeout, server hub not responding.") get a date too.
    for line in iter(stream.readline, b""):
        text = line.decode(errors="replace").rstrip()
        if text:
            log("ssh: " + text)


def serve():
    configured = hubs()
    local = configured[0][0]
    key = secret()
    RUN.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(RUN, 0o700)
    lock = (RUN / "service.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    endpoint = RUN / "receiver.sock"
    if endpoint.exists():
        if not endpoint.is_socket():
            raise ValueError("receiver path exists and is not a socket")
        endpoint.unlink()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    with Server(endpoint, local, key) as server:
        os.chmod(endpoint, 0o600)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        tunnels = [threading.Thread(target=keep_tunnel, args=(hub, stop, len(configured) > 1), daemon=True)
                   for hub in configured]
        for tunnel in tunnels:
            tunnel.start()
        try:
            while not stop.wait(1):
                trim_log(RUN / "service.log")
        finally:
            stop.set()
            for tunnel in tunnels:
                tunnel.join(timeout=10)
            server.shutdown()
            thread.join()
    endpoint.unlink(missing_ok=True)


def keep_tunnel(hub, stop, many):
    local, relay, alias, root, peers = hub
    pid = RUN / (("tunnel-" + relay + ".pid") if many else "tunnel.pid")
    tag = (relay + ": ") if many else ""
    failures = 0
    while not stop.is_set():
        process = None
        started = None
        problem = None
        try:
            prepare_hub(local, alias, root)
            process = subprocess.Popen(tunnel_command(local, relay, alias, root, peers),
                                       stderr=subprocess.PIPE)
            started = time.monotonic()
            threading.Thread(target=pump_stderr, args=(process.stderr,), daemon=True).start()
            pid.write_text(str(process.pid) + "\n")
            while process.poll() is None and not stop.wait(1):
                pass
        except (OSError, subprocess.SubprocessError) as error:
            problem = failure_summary(error)
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            pid.unlink(missing_ok=True)
        if stop.is_set():
            break
        lived = time.monotonic() - started if started is not None else 0
        # A tunnel that held for 30s was a real connection; start the backoff over.
        failures = 0 if lived >= 30 else failures + 1
        delay = reconnect_delay(max(failures, 1))
        if problem is None:
            log(tag + "relay tunnel disconnected after " + str(int(lived)) + "s; reconnecting in " + str(delay) + "s")
        else:
            log(tag + "relay connection failed (" + str(failures) + "x): " + problem + "; retrying in " + str(delay) + "s")
        stop.wait(delay)


def install():
    hubs()  # Validate before touching service definitions.
    secret()
    RUN.mkdir(parents=True, exist_ok=True, mode=0o700)
    argv = [peer_core.python(), str(Path(__file__).resolve().with_name("peer-relay")), "serve"]
    env = {"PATH": os.environ["PATH"], "PEER_CONFIG": str(CONFIG), "PEER_STATE": str(peer_core.root())}
    if sys.platform == "darwin":
        dest = Path.home() / ("Library/LaunchAgents/" + LABEL + ".plist")
        dest.parent.mkdir(parents=True, exist_ok=True)
        spec = {"Label": LABEL, "ProgramArguments": argv, "EnvironmentVariables": env,
                "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 5,
                "StandardOutPath": str(RUN / "service.log"), "StandardErrorPath": str(RUN / "service.log")}
        dest.write_bytes(plistlib.dumps(spec))
        domain = "gui/" + str(os.getuid())
        subprocess.run(["launchctl", "bootout", domain + "/" + LABEL], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        # bootout can return before launchd releases the old job. Retry its
        # transient EIO without hiding a persistent bootstrap failure.
        for attempt in range(6):
            boot = subprocess.run(["launchctl", "bootstrap", domain, str(dest)], capture_output=True, text=True)
            if boot.returncode == 0:
                break
            if attempt == 5:
                raise ValueError('relay bootstrap failed: ' + boot.stderr.strip() +
                                 '; retry: launchctl bootstrap ' + domain + ' ' + str(dest))
            time.sleep(.25 * (attempt + 1))
    else:
        dest = Path.home() / ".config/systemd/user/peer-relay.service"
        dest.parent.mkdir(parents=True, exist_ok=True)
        # systemd accepts quoted strings, but expands percent specifiers itself.
        quote = lambda x: json.dumps(x).replace("%", "%%")
        content = ("[Unit]\nDescription=peer outbound relay tunnel\nAfter=network.target\n\n"
                   "[Service]\nType=simple\nExecStart=" + " ".join(quote(x) for x in argv) + "\n" +
                   "".join("Environment=" + quote(k + "=" + v) + "\n" for k, v in env.items()) +
                   "Restart=always\nRestartSec=3\n\n[Install]\nWantedBy=default.target\n")
        dest.write_text(content)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "peer-relay.service"], check=True)
        subprocess.run(["systemctl", "--user", "restart", "peer-relay.service"], check=True)
    print("installed " + str(dest))


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    if command == "call":
        result = call(sys.argv[2], sys.argv[3], sys.argv[4:])
        sys.stdout.write(result.get("out", ""))
        sys.stderr.write(result.get("err", ""))
        return result["code"]
    if command == "serve":
        serve()
    elif command == "install":
        install()
    elif command == "keygen":
        if KEY.exists():
            raise ValueError(str(KEY) + " already exists; delete it first to rotate (then recopy to every owner)")
        fd = os.open(str(KEY), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_hex(32) + "\n")
        print("created " + str(KEY) + "; copy it to each owner machine (mode 600), never to the hub")
    elif command == "status":
        code = 0
        for local, relay, alias, root, peers in hubs():
            print("host=" + local + " relay=" + relay + " ssh=" + alias + " hub=" + root)
            for host in peers:
                try:
                    result = call(relay, host, ['_rpc', local, json.dumps({'op': 'ping'})])
                    print(host + ": " + ("OK" if result["code"] == 0 else "FAILED " + result.get("err", "") + result.get("out", "")))
                    code |= bool(result["code"])
                except (OSError, ValueError) as error:
                    print(host + ": FAILED " + str(error))
                    code = 1
        return code
    else:
        raise ValueError("usage: peer relay [status|keygen|install|serve|call HUB HOST ARGS...]")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        sys.exit("error: relay: " + str(error))
