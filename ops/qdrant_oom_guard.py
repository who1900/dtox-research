#!/usr/bin/env python3
"""Protect the verified qdrant main process or its sole direct qdrant child."""
import contextlib
import json
import os
import re
import subprocess
import sys

SCORE = -900
DOCKER = "/usr/bin/docker"


class GuardError(RuntimeError):
    pass


def container_identity():
    try:
        result = subprocess.run(
            [DOCKER, "--host", "unix:///var/run/docker.sock", "container", "inspect",
             "--format", '{"Id":{{json .Id}},"Name":{{json .Name}},"State":{{json .State}}}',
             "qdrant"],
            capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GuardError("local Docker inspect unavailable") from exc
    if result.returncode:
        raise GuardError("cannot inspect the local qdrant container")
    try:
        data = json.loads(result.stdout)
        state = data["State"]
        pid = state["Pid"]
        cid = data["Id"]
        valid = (data["Name"] == "/qdrant" and state["Running"] is True
                 and state.get("Paused") is False and state.get("Restarting") is False
                 and state.get("Dead") is False and state.get("Status") == "running"
                 and type(pid) is int and pid > 1
                 and isinstance(cid, str) and re.fullmatch(r"[0-9a-f]{64}", cid))
    except (KeyError, TypeError, ValueError) as exc:
        raise GuardError("invalid qdrant inspect response") from exc
    if not valid:
        raise GuardError("qdrant name/state/main PID validation failed; no write")
    return cid, pid


def read_proc(directory, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    try:
        raw = os.read(fd, 4097)
        if len(raw) > 4096:
            raise GuardError("oversized proc identity; no write")
        return raw.decode("ascii").strip()
    finally:
        os.close(fd)


def process_identity(directory, pid, cid):
    comm = read_proc(directory, "comm")
    stat = read_proc(directory, "stat")
    try:
        head, fields = stat.rsplit(")", 1)
        if not comm or head != f"{pid} ({comm}":
            raise ValueError("wrong stat identity")
        fields = fields.split()
        if fields[0] in ("Z", "X", "x"):
            raise ValueError("dead process")
        ppid, started = int(fields[1]), int(fields[19])
        groups = []
        matching_container = False
        for line in read_proc(directory, "cgroup").splitlines():
            hierarchy, controllers, path = line.split(":", 2)
            if not hierarchy.isdigit() or not path.startswith("/"):
                raise ValueError("invalid cgroup")
            groups.append((hierarchy, controllers, path))
            matching_container |= any(part in (cid, f"docker-{cid}.scope") for part in path.split("/"))
        if not matching_container:
            raise GuardError("process cgroup does not match exact container ID; no write")
        return comm, ppid, started, tuple(sorted(groups))
    except (ValueError, IndexError) as exc:
        raise GuardError("invalid/dead qdrant process identity; no write") from exc


def open_process(stack, pid):
    directory = os.open(f"/proc/{pid}", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    stack.callback(os.close, directory)
    return directory


def direct_qdrant_child(stack, parent, main_pid, cid, parent_identity):
    raw = read_proc(parent, f"task/{main_pid}/children")
    try:
        children = [int(part) for part in raw.split()]
        if len(children) > 64 or len(children) != len(set(children)) or any(pid <= 1 for pid in children):
            raise ValueError("invalid children")
    except ValueError as exc:
        raise GuardError("invalid direct-child list; no write") from exc
    candidates = []
    for pid in children:
        directory = open_process(stack, pid)
        if read_proc(directory, "comm") != "qdrant":
            continue
        info = process_identity(directory, pid, cid)
        if info[1] != main_pid or info[3] != parent_identity[3]:
            raise GuardError("qdrant child PPID/cgroup mismatch; no write")
        candidates.append((pid, directory, info))
    if len(candidates) != 1:
        raise GuardError("wrapper requires exactly one direct qdrant child; no write")
    return candidates[0]


def protect_qdrant():
    if sys.platform != "linux":
        raise GuardError("Linux host /proc required; no write")
    identity = container_identity()
    cid, main_pid = identity
    with contextlib.ExitStack() as stack:
        parent = open_process(stack, main_pid)
        parent_info = process_identity(parent, main_pid, cid)
        if parent_info[0] == "qdrant":
            pid, directory, target_info = main_pid, parent, parent_info
        else:
            pid, directory, target_info = direct_qdrant_child(stack, parent, main_pid, cid, parent_info)
        # Pin the proc inode: a recycled PID cannot redirect this descriptor.
        fd = os.open("oom_score_adj", os.O_RDWR | os.O_NOFOLLOW, dir_fd=directory)
        stack.callback(os.close, fd)
        if container_identity() != identity or process_identity(parent, main_pid, cid) != parent_info:
            raise GuardError("qdrant container/main PID changed during validation; no write")
        if pid != main_pid:
            checked_pid, _checked_directory, checked_info = direct_qdrant_child(
                stack, parent, main_pid, cid, parent_info)
            if checked_pid != pid or checked_info != target_info:
                raise GuardError("qdrant direct child changed during validation; no write")
        if process_identity(directory, pid, cid) != target_info:
            raise GuardError("qdrant target PID changed during validation; no write")
        current = int(os.read(fd, 64).decode("ascii").strip())
        if current != SCORE:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.write(fd, f"{SCORE}\n".encode("ascii")) != len(f"{SCORE}\n"):
                raise GuardError("short oom_score_adj write")
        os.lseek(fd, 0, os.SEEK_SET)
        if int(os.read(fd, 64).decode("ascii").strip()) != SCORE:
            raise GuardError("oom_score_adj verification failed")
    return pid


def main():
    try:
        pid = protect_qdrant()
    except (GuardError, OSError, ValueError) as exc:
        print(f"qdrant OOM guard failed: {exc}", file=sys.stderr)
        return 1
    print(f"verified qdrant PID {pid}: oom_score_adj={SCORE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
