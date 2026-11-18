#!/usr/bin/env python3
"""Run the one-camera Bruck setup from Ubuntu/WSL."""

###  cd "$HOME/Bruck" && ./Ubuntu_Notes/run-cam0.py

import argparse
from contextlib import ExitStack
import os
from pathlib import Path
from queue import Empty, Queue
import re
import shlex
import signal
import socket
import subprocess
import tempfile
from threading import Thread
import time
from urllib.parse import urlsplit, urlunsplit


CONFIG = """\
[mainbrain]
http_api_server_addr = "0.0.0.0:8396"
model_server_addr = "127.0.0.1:8397"
lowlatency_camdata_udp_port = 47912
output_base_dirname = "~/BRAID-DATA"

[trigger]
trigger_type = "FakeSync"
framerate = 30.0

[[cameras]]
name = "cam0"
start_backend = "remote"
http_server_addr = "127.0.0.1:36055"
"""

# SSH stdout carries length-prefixed UDP datagrams. Camera logs use stderr.
BOARD_PROGRAM = """\
import os
import select
import signal
import socket
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import ProxyHandler, build_opener


def stop(*_):
    raise SystemExit(0)


signal.signal(signal.SIGHUP, stop)
signal.signal(signal.SIGTERM, stop)

binary = "/home/root/strand-cam"
if not os.access(binary, os.X_OK):
    raise SystemExit("Missing executable on MP157: " + binary)

opener = build_opener(ProxyHandler({}))
try:
    with opener.open("http://127.0.0.1:18396/", timeout=5):
        pass
except HTTPError as error:
    if error.code != 401:
        raise

with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
    udp.bind(("127.0.0.1", 47912))
    sys.stdout.buffer.write(b"\\0\\0")
    sys.stdout.buffer.flush()

    url = sys.stdin.readline().strip()
    if not url:
        sys.exit(0)
    camera = subprocess.Popen(
        [binary, "--no-browser", "--camera-backend", "webcam",
         "--camera-name", "0", "--camera-id", "cam0", "--braid-url", url,
         "--capture-width", "640", "--capture-height", "400", "--capture-fps", "30"],
        stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr,
        env=dict(os.environ, DISABLE_VERSION_CHECK="1"),
    )
    try:
        while camera.poll() is None:
            readable, _, _ = select.select([sys.stdin, udp], [], [], 0.5)
            if sys.stdin in readable:
                break
            if udp in readable:
                packet, _ = udp.recvfrom(65535)
                if packet:
                    sys.stdout.buffer.write(len(packet).to_bytes(2, "big") + packet)
                    sys.stdout.buffer.flush()
        if camera.poll() is not None:
            raise SystemExit("strand-cam exited with status {}".format(camera.returncode))
    finally:
        if camera.poll() is None:
            camera.send_signal(signal.SIGINT)
            try:
                camera.wait(timeout=4)
            except subprocess.TimeoutExpired:
                camera.kill()
                camera.wait()
"""

TOKEN_URL = re.compile(r"--braid-url (https?://\S+)")
TOKEN_VALUE = re.compile(r"([?&]token=)[^&\s\"']+")
QR_CHARACTERS = frozenset(" ▀▄█▌▐")


def show(message):
    print(message, flush=True)


def relay_logs(stream, label, events, find_url=False):
    for raw in iter(stream.readline, b""):
        line = raw.decode(errors="replace").strip()
        if find_url and 'Not starting remote camera "cam0"' in line:
            match = TOKEN_URL.search(line)
            if match:
                events.put(("url", match.group(1)))
        if find_url and "Braid HTTP server listening at" in line:
            events.put(("braid-ready", None))
        if label == "MP157":
            if "acquired first frame: 640x400" in line:
                events.put(("first-frame", None))
            if "Registered camera with Braid" in line:
                events.put(("registered", None))
        if line and not line.startswith("QR code for ") and not set(line) <= QR_CHARACTERS:
            show(f"[{label}] {TOKEN_VALUE.sub(r'\g<1><redacted>', line)}")


def relay_camera(stream, events):
    try:
        if stream.read(2) != b"\0\0":
            raise RuntimeError("MP157 relay did not report readiness")
        events.put(("relay-ready", None))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            while header := stream.read(2):
                if len(header) != 2:
                    raise RuntimeError("truncated UDP relay header")
                size = int.from_bytes(header, "big")
                packet = stream.read(size)
                if len(packet) != size:
                    raise RuntimeError("truncated UDP relay datagram")
                udp.sendto(packet, ("127.0.0.1", 47912))
    except (OSError, RuntimeError) as error:
        events.put(("relay-error", str(error)))


def start_thread(function, *args):
    Thread(target=function, args=args, daemon=True).start()


def stop_process(process, grace=0, initial_signal=signal.SIGTERM):
    if process.poll() is None and grace == 0:
        process.send_signal(initial_signal)
    try:
        process.wait(timeout=grace or 5)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def stop_ssh(process):
    try:
        process.stdin.close()
    except BrokenPipeError:
        pass
    stop_process(process, grace=6)


def wait_for(events, processes, expected, timeout):
    received = {}
    deadline = time.monotonic() + timeout if timeout is not None else None
    while True:
        if deadline is not None and time.monotonic() >= deadline:
            missing = ", ".join(sorted(expected - received.keys()))
            raise RuntimeError(f"timed out waiting for {missing}")
        try:
            name, value = events.get(timeout=0.25)
        except Empty:
            pass
        else:
            if name == "relay-error":
                raise RuntimeError(value)
            received[name] = value
        for label, process in processes.items():
            if process.poll() is not None:
                raise RuntimeError(f"{label} exited with status {process.returncode}")
        if expected and expected <= received.keys():
            return received


def local_url(original, port):
    parsed = urlsplit(original)
    return urlunsplit((parsed.scheme, f"127.0.0.1:{port}", parsed.path or "/", parsed.query, ""))


def run(board, braid_binary, repo):
    if not os.access(braid_binary, os.X_OK):
        raise RuntimeError(f"build braid-run first: {braid_binary}")
    events = Queue()
    with ExitStack() as stack:
        temporary = stack.enter_context(tempfile.TemporaryDirectory(prefix="bruck-cam0-"))
        config = Path(temporary) / "cam0.toml"
        config.write_text(CONFIG, encoding="utf-8")

        braid = subprocess.Popen(
            [str(braid_binary), str(config)], cwd=repo,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=dict(os.environ, DISABLE_VERSION_CHECK="1"), start_new_session=True,
        )
        stack.callback(stop_process, braid, initial_signal=signal.SIGINT)
        start_thread(relay_logs, braid.stdout, "Braid", events, True)
        processes = {"Braid": braid}
        startup = wait_for(events, processes, {"url", "braid-ready"}, 30)

        show("Opening SSH to the MP157. Enter the board password if prompted.")
        ssh = subprocess.Popen(
            ["ssh", "-T", "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=10",
             "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3",
             "-R", "127.0.0.1:18396:127.0.0.1:8396",
             "-L", "127.0.0.1:36055:127.0.0.1:36055",
             "--", board, shlex.join(["python3", "-u", "-c", BOARD_PROGRAM])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        stack.callback(stop_ssh, ssh)
        processes["MP157 SSH"] = ssh
        start_thread(relay_logs, ssh.stderr, "MP157", events)
        start_thread(relay_camera, ssh.stdout, events)
        wait_for(events, processes, {"relay-ready"}, 90)

        try:
            ssh.stdin.write((local_url(startup["url"], 18396) + "\n").encode())
            ssh.stdin.flush()
        except BrokenPipeError as error:
            raise RuntimeError("MP157 SSH closed before camera startup") from error

        show("Waiting for the first frame and camera registration...")
        wait_for(events, processes, {"first-frame", "registered"}, 45)
        show("cam0 is capturing and registered. Press Ctrl+C here to stop the entire session.")
        show(f"Windows browser (private login URL): {local_url(startup['url'], 8396)}")
        wait_for(events, processes, set(), None)


def main():
    repo = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", default="root@10.4.8.64", help="MP157 SSH destination")
    parser.add_argument(
        "--braid-bin", type=Path,
        default=repo / "target/x86_64-unknown-linux-gnu/release/braid-run",
        help="path to the native braid-run executable",
    )
    args = parser.parse_args()

    def interrupt(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    try:
        run(args.board, args.braid_bin.resolve(), repo)
    except KeyboardInterrupt:
        show("Stopped Bruck, camera, and SSH relay.")
    except (OSError, RuntimeError) as error:
        parser.exit(1, f"Startup or runtime error: {error}\n")


if __name__ == "__main__":
    main()
