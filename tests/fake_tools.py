#!/usr/bin/env python3
"""Isolated host/Telegram doubles; age and tar use the real installed binaries.

GNU command-line adapters let the orchestration tests also run on macOS. These
doubles never connect to Docker, Telegram, or change a real service/account.
"""
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

tool = Path(sys.argv[0]).name
args = sys.argv[1:]
root = Path(os.environ["FAKE_ROOT"])
state_file = root / "state.json"


def state():
    return json.loads(state_file.read_text())


def save(value):
    state_file.write_text(json.dumps(value))


def event(kind, **fields):
    with (root / "events.jsonl").open("a") as stream:
        stream.write(json.dumps({"event": kind, **fields}, ensure_ascii=False) + "\n")


def run(command):
    return subprocess.call(command)


if tool == "id":
    print("0" if args == ["-u"] or args[-1] == "root" else "1000")
elif tool == "stat":
    info = os.stat(args[-1])
    field = args[1]
    print({"%u": 0, "%a": format(info.st_mode & 0o7777, "o"),
           "%s": info.st_size, "%Y": int(info.st_mtime)}[field])
elif tool == "realpath":
    path = Path(args[-1])
    if "-e" in args and not path.exists():
        sys.exit(1)
    print(path.resolve())
elif tool == "du":
    total = sum(p.stat().st_size for p in Path(args[-1]).rglob("*") if p.is_file())
    print(f"{total}\t{args[-1]}")
elif tool == "df":
    print("Avail")
    print(1 if os.environ.get("FAIL_DISK") else 10**12)
elif tool == "flock":
    try:
        fcntl.flock(int(args[-1]), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(1)
elif tool == "timeout":
    # Real commands are short; no actual service commands are used in this suite.
    sys.exit(run(args[2:]))
elif tool == "setsid":
    os.setsid()
    os.execvp(args[1], args[1:])  # Skip GNU --wait; exec preserves the leader PID.
elif tool == "runuser":
    sys.exit(run(args[args.index("--") + 1:]))
elif tool == "sleep":
    event("sleep", seconds=float(args[0]))
    if float(args[0]) == 5:
        time.sleep(0.05)  # Let Bash's readiness deadline advance in failure tests.
elif tool == "sha256sum":
    import hashlib
    if not args:
        print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest() + "  -")
    else:
        for arg in args:
            if arg != "--":
                print(hashlib.sha256(Path(arg).read_bytes()).hexdigest() + "  " + arg)
elif tool == "split":
    size = int(next(a.split("=", 1)[1] for a in args if a.startswith("--bytes=")))
    source, prefix = args[-2:]
    with open(source, "rb") as stream:
        index = 1
        while True:
            chunk = stream.read(size)
            if not chunk:
                break
            Path(f"{prefix}{index:06d}").write_bytes(chunk)
            index += 1
elif tool == "tee":
    with open(args[0], "wb") as stream:
        while True:
            chunk = sys.stdin.buffer.read(8192)
            if not chunk:
                break
            if os.environ.get("FAIL_WRITE"):
                stream.write(chunk[:10])
                sys.exit(1)  # Simulate ENOSPC after the preflight check.
            stream.write(chunk)
            sys.stdout.buffer.write(chunk)
elif tool == "tar":
    if args == ["--version"]:
        print("GNU tar (test adapter, actual archive by system tar)")
    else:
        event("archive")
        if os.environ.get("FAIL_TAR"):
            sys.exit(2)
        if os.environ.get("SIGNAL_TAR"):
            os.kill(os.getppid(), signal.SIGTERM)
        if os.environ.get("HOLD_TAR"):
            Path(os.environ["HOLD_TAR"]).touch()
            time.sleep(1)
        portable = [a for a in args if a not in ("--sparse", "--acls", "--xattrs")]
        sys.exit(run([os.environ["REAL_TAR"], *portable]))
elif tool == "age":
    if "-o" not in args and os.environ.get("FAIL_AGE"):
        sys.stdin.buffer.read()
        sys.exit(1)
    sys.exit(run([os.environ["REAL_AGE"], *args]))
elif tool == "bitwarden.sh":
    current = state()
    command = args[0]
    event(command)
    if command == "stop":
        if not os.environ.get("STOP_LIES"):
            current["running"] = False
        save(current)
        sys.exit(1 if os.environ.get("FAIL_STOP") else 0)
    if command == "start":
        if os.environ.get("FAIL_START") or os.environ.get("FAIL_BW_START"):
            sys.exit(1)
        current["running"] = True
        current["restarted"] = True
        save(current)
elif tool == "docker":
    current = state()
    if args[0] == "info" or args[:2] == ["compose", "version"]:
        sys.exit(0)
    if args[0] == "compose":
        if "config" in args:
            print(json.dumps({"services": {
                "mssql": {"container_name": "bitwarden-mssql"},
                "nginx": {"container_name": "bitwarden-nginx"}}}))
        elif "up" in args:
            event("fallback-start", argv=args)
            if os.environ.get("FAIL_START"):
                sys.exit(1)
            current["running"] = True
            current["restarted"] = True
            save(current)
        else:
            sys.exit(2)
    elif args[0] == "inspect":
        if not current["running"]:
            sys.exit(1)
        healthy = not (os.environ.get("FAIL_HEALTH") and current.get("restarted"))
        source = root / "install" / "bwdata"
        if os.environ.get("EXTERNAL_MOUNT"):
            source = root / "external"
        elif os.environ.get("SYMLINK_MOUNT"):
            source = source / "external-link"
        environment = (root / "install/bwdata/env/global.override.env").read_text().splitlines()
        print(json.dumps([{
            "Name": "/" + name,
            "Config": {"Env": environment},
            "State": {"Running": True, "Health": {"Status": "healthy" if healthy else "unhealthy"}},
            "Mounts": [{"Type": "bind", "Source": str(source), "RW": True,
                        "Destination": "/tmp" if os.environ.get("MISSING_DB_MOUNT") else "/var/opt/mssql/data"}],
        } for name in args[1:]]))
    elif args[0] == "ps":
        print("bitwarden-mssql\nbitwarden-nginx" if current["running"] else "")
    else:
        sys.exit(2)
elif tool == "curl":
    config = sys.stdin.read()
    token = os.environ["TEST_TOKEN"]
    assert token in config, "Token must be passed on stdin"
    assert all(token not in a for a in args), "Token leaked in process arguments"
    assert "TELEGRAM_BOT_TOKEN" not in os.environ, "Token leaked in child environment"
    method = re.search(r"/([^/\"]+)\"\s*$", config).group(1)
    output = Path(args[args.index("--output") + 1])
    text, document, document_bytes = "", "", ""
    for i, arg in enumerate(args):
        if arg == "--form" and args[i + 1].startswith("text=<"):
            text = Path(args[i + 1][len('text=<"'):-1]).read_text()
        if arg == "--form" and args[i + 1].startswith("document=@"):
            path = Path(args[i + 1][len('document=@"'):-1])
            document = path.name
            destination = root / "uploads" / path.name
            destination.parent.mkdir(exist_ok=True)
            shutil.copyfile(path, destination)
            document_bytes = str(path.stat().st_size)
    current = state()
    failure = os.environ.get("FAIL_MESSAGE", "")
    is_upload_failure = method == "sendDocument" and (
        os.environ.get("FAIL_UPLOAD") or
        (os.environ.get("FAIL_MANIFEST") and document == "manifest.json"))
    is_message_failure = method == "sendMessage" and failure and failure in text
    code, rc = 200, 0
    if is_upload_failure or is_message_failure:
        code = 403
    if os.environ.get("RETRY_ONCE") and method == "sendDocument" and not current.get("retried"):
        current["retried"] = True
        code = int(os.environ["RETRY_ONCE"]) if os.environ["RETRY_ONCE"] != "network" else 0
        rc = 6 if code == 0 else 0
        save(current)
    if os.environ.get("FAIL_NETWORK") and method.startswith("send"):
        code, rc = 0, 6
    event("telegram", method=method, text=text, document=document, bytes=document_bytes, code=code)
    if code == 200:
        result = {"id": 123456789, "type": "private"}
        output.write_text(json.dumps({"ok": True, "result": result}))
    else:
        output.write_text(json.dumps({"ok": False, "error_code": code, "parameters": {"retry_after": 2}}))
    print(f"{code:03d}", end="")
    sys.exit(rc)
else:
    raise RuntimeError(f"Unexpected fake tool: {tool} {args}")
