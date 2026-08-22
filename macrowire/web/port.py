"""Who holds a TCP port, and how to stop our own server on it.

Stdlib only: /proc/net/tcp gives the socket inode for a listening address,
and /proc/<pid>/fd holds symlinks naming that inode. No lsof, no ss, no
psutil.

This exists because `pkill -f uvicorn` is a trap. Pattern-matching on a
command line matches the shell that ran the grep, sibling processes, and
anything else that happens to mention the string - which in practice means
killing your own terminal while the server keeps running. Resolving a PID
from the port and checking it is actually ours is the safe version.
"""

from __future__ import annotations

import os
import signal
import socket
from pathlib import Path

PROC_NET = ("/proc/net/tcp", "/proc/net/tcp6")
LISTEN = "0A"          # TCP_LISTEN in /proc/net/tcp


def _listening_inodes(port: int) -> set[str]:
    """Inodes of sockets LISTENing on `port`, any local address."""
    found: set[str] = set()
    for path in PROC_NET:
        try:
            lines = Path(path).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 10 or parts[3] != LISTEN:
                continue
            local = parts[1]
            try:
                bound = int(local.rsplit(":", 1)[1], 16)
            except (IndexError, ValueError):
                continue
            if bound == port:
                found.add(parts[9])
    return found


def holder(port: int) -> dict | None:
    """The process listening on `port`, or None.

    Only processes readable by this user resolve to a PID; a port held by
    another user reports the inode without one rather than pretending the
    port is free.
    """
    inodes = _listening_inodes(port)
    if not inodes:
        return None

    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        fd_dir = entry / "fd"
        try:
            handles = list(fd_dir.iterdir())
        except OSError:
            continue                      # not ours, or gone between calls
        for handle in handles:
            try:
                target = os.readlink(handle)
            except OSError:
                continue
            if not target.startswith("socket:["):
                continue
            if target[8:-1] in inodes:
                pid = int(entry.name)
                try:
                    cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode().strip()
                except OSError:
                    cmdline = ""
                return {"pid": pid, "cmdline": cmdline,
                        "is_macrowire": "macrowire" in cmdline,
                        "is_self": pid == os.getpid()}
    return {"pid": None, "cmdline": "", "is_macrowire": False, "is_self": False}


def is_free(port: int, host: str = "127.0.0.1") -> bool:
    """Can the server actually bind here?

    SO_REUSEADDR is set because uvicorn sets it. Without it this probe is
    STRICTER than the thing it is checking for: a socket lingering in a
    closing state fails a plain bind but not a SO_REUSEADDR one, so the
    preflight would refuse to start a server that would have started fine.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def stop(port: int, timeout: float = 5.0) -> dict:
    """Stop the MacroWire server on `port`. Refuses anything else.

    Never matches on a command-line pattern: the PID comes from the port,
    and is killed only after confirming it is a macrowire process and not
    this one.
    """
    import time

    found = holder(port)
    if found is None:
        # A machine-readable code alongside the prose. The caller decides an
        # exit status from this, and deciding it by searching English inside
        # a sentence breaks the moment the sentence is translated.
        return {"stopped": False, "code": "not_listening",
                "reason": f"nothing is listening on {port}"}
    if found["pid"] is None:
        return {"stopped": False, "code": "uninspectable",
                "reason": f"port {port} is held by a process this user cannot inspect"}
    if found["is_self"]:
        return {"stopped": False, "code": "self",
                "reason": "that is this process"}
    if not found["is_macrowire"]:
        return {"stopped": False, "code": "not_ours", "pid": found["pid"],
                "cmdline": found["cmdline"],
                "reason": f"pid {found['pid']} on port {port} is not a macrowire "
                          f"server; refusing to kill it"}

    pid = found["pid"]
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # Wait for the PORT to be bindable, not merely for the process to
        # go: the socket outlives the process briefly, and returning early
        # makes an immediate restart fail for no reason.
        if holder(port) is None and is_free(port):
            return {"stopped": True, "pid": pid, "signal": "SIGTERM"}
        time.sleep(0.15)

    os.kill(pid, signal.SIGKILL)          # declined to go quietly
    time.sleep(0.4)
    gone = holder(port) is None
    if gone:
        return {"stopped": True, "pid": pid, "signal": "SIGKILL"}
    # SIGKILL and still holding the port. The caller printed result["reason"]
    # unconditionally, so returning without one raised a KeyError on the one
    # path where something is actually wrong.
    return {"stopped": False, "code": "survived_sigkill", "pid": pid,
            "signal": "SIGKILL",
            "reason": f"pid {pid} still holds port {port} after SIGKILL"}
