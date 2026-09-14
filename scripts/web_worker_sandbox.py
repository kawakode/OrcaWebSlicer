#!/usr/bin/env python3
"""Confine one worker process to the least privilege its job needs.

The executor owns bounded resources; this module owns everything else a
compromised or runaway worker must not reach: the network, the ambient
environment, a shared temporary directory, inherited capabilities, and the
privileges of the service that launched it.

Namespaces are deliberately not the mechanism. Under the canonical container
runtime every `unshare` flag fails with `EPERM`, including `CLONE_NEWNET` as
root, because the default seccomp profile refuses them without `CAP_SYS_ADMIN`;
see adr/0003-worker-sandbox.md. Installing a seccomp filter needs no privilege
at all once `no_new_privs` is set, so network access is denied by refusing the
address families that leave the host rather than by building a namespace the
runtime will not grant.
"""

from __future__ import annotations

import ctypes
import dataclasses
import errno
import os
import socket
import struct
import sys
from functools import lru_cache
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


class SandboxError(RuntimeError):
    """A stable sandbox failure that is safe to return to the API layer."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# Only variables the worker itself documents as inputs survive; everything else
# in a long-lived service's environment is somebody's credential.
ENVIRONMENT_ALLOWLIST = ("LANG", "LC_ALL", "ORCA_SLICER_RESOURCES", "PATH", "TZ")
ENVIRONMENT_ALLOWED_PREFIXES = ("ORCA_WEB_MAX_",)
PRIVATE_TMP_NAME = "tmp"

_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_PR_CAPBSET_DROP = 24
_PR_CAP_AMBIENT = 47
_PR_CAP_AMBIENT_CLEAR_ALL = 4
_SECCOMP_MODE_FILTER = 2

_BPF_LD_ABS_W = 0x20
_BPF_JEQ_K = 0x15
_BPF_JGE_K = 0x35
_BPF_RET_K = 0x06
_SECCOMP_RET_ERRNO = 0x00050000
_SECCOMP_RET_ALLOW = 0x7FFF0000
_SECCOMP_RET_KILL_PROCESS = 0x80000000

# struct seccomp_data: int nr, __u32 arch, __u64 instruction_pointer, __u64 args[6]
_NR_OFFSET = 0
_ARCH_OFFSET = 4
_ARG0_OFFSET = 16
_X32_SYSCALL_BIT = 0x40000000

# (AUDIT_ARCH, __NR_socket, guard against the x32 ABI reusing AUDIT_ARCH_X86_64)
_SECCOMP_ARCHITECTURES = {
    "x86_64": (0xC000003E, 41, True),
    "aarch64": (0xC00000B7, 198, False),
}

_SOCK_FILTER = struct.Struct("HBBI")
_SOCK_FPROG = struct.Struct("HP")


@dataclasses.dataclass(frozen=True)
class SandboxPolicy:
    """What must hold before a worker is allowed to start.

    `required` is the default: a control that cannot be applied fails the job
    rather than starting a worker with more reach than the deployment promised.
    `off` exists for platforms and development hosts that cannot provide the
    Linux controls at all, and is never the default.
    """

    mode: str = "required"
    user: Optional[Tuple[int, int]] = None
    private_tmp: bool = True

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def validate(self) -> None:
        if self.mode not in {"required", "off"}:
            raise SandboxError("invalid_sandbox_mode", "Sandbox mode must be 'required' or 'off'.")
        if self.user is not None:
            uid, gid = self.user
            if uid <= 0 or gid <= 0:
                raise SandboxError(
                    "invalid_sandbox_user", "A worker user must be a non-root uid and gid."
                )


@dataclasses.dataclass(frozen=True)
class SandboxSetup:
    """The prepared confinement for exactly one worker launch."""

    environment: Dict[str, str]
    child_setup: Optional[Callable[[], None]]
    controls: Tuple[str, ...]

    def describe(self) -> Dict[str, object]:
        return {"controls": list(self.controls)}


def _parse_user(value: str) -> Tuple[int, int]:
    text = value.strip()
    parts = text.split(":", 1)
    try:
        uid = int(parts[0])
        gid = int(parts[1]) if len(parts) == 2 and parts[1] else uid
    except ValueError as error:
        raise SandboxError(
            "invalid_sandbox_user", "ORCA_WEB_WORKER_USER must be numeric 'uid[:gid]'."
        ) from error
    return uid, gid


def policy_from_environment(defaults: Optional[SandboxPolicy] = None) -> SandboxPolicy:
    defaults = defaults or SandboxPolicy()
    mode = os.environ.get("ORCA_WEB_WORKER_SANDBOX", "").strip() or defaults.mode
    configured_user = os.environ.get("ORCA_WEB_WORKER_USER", "").strip()
    policy = SandboxPolicy(
        mode=mode,
        user=_parse_user(configured_user) if configured_user else defaults.user,
        private_tmp=defaults.private_tmp,
    )
    policy.validate()
    return policy


def _seccomp_network_filter() -> bytes:
    """Deny every socket domain that can leave the host, keeping `AF_UNIX`.

    Blocking the one entry point is enough because the worker starts with no
    inherited descriptors, so it has no socket it did not create itself.
    """
    architecture = _SECCOMP_ARCHITECTURES.get(os.uname().machine)
    if architecture is None:
        raise SandboxError(
            "sandbox_unsupported_architecture",
            "Worker network confinement has no filter for this architecture.",
        )
    audit_arch, socket_nr, guard_x32 = architecture

    body: List[Tuple[int, object, object, int]] = [
        (_BPF_LD_ABS_W, 0, 0, _ARCH_OFFSET),
        (_BPF_JEQ_K, 0, "kill", audit_arch),
        (_BPF_LD_ABS_W, 0, 0, _NR_OFFSET),
    ]
    if guard_x32:
        body.append((_BPF_JGE_K, "kill", 0, _X32_SYSCALL_BIT))
    body += [
        (_BPF_JEQ_K, 0, "allow", socket_nr),
        (_BPF_LD_ABS_W, 0, 0, _ARG0_OFFSET),
        (_BPF_JEQ_K, "allow", 0, socket.AF_UNIX),
    ]
    # The three terminal returns are appended below in this order.
    labels = {"allow": len(body) + 1, "kill": len(body) + 2}

    def jump(target: object, index: int) -> int:
        return 0 if target == 0 else labels[str(target)] - index - 1

    program = [
        _SOCK_FILTER.pack(code, jump(jt, index), jump(jf, index), k)
        for index, (code, jt, jf, k) in enumerate(body)
    ]
    program += [
        _SOCK_FILTER.pack(_BPF_RET_K, 0, 0, _SECCOMP_RET_ERRNO | errno.EAFNOSUPPORT),
        _SOCK_FILTER.pack(_BPF_RET_K, 0, 0, _SECCOMP_RET_ALLOW),
        _SOCK_FILTER.pack(_BPF_RET_K, 0, 0, _SECCOMP_RET_KILL_PROCESS),
    ]
    return b"".join(program)


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL("libc.so.6", use_errno=True)


def _prctl(library: ctypes.CDLL, option: int, *arguments: object) -> None:
    padded = list(arguments) + [0] * (4 - len(arguments))
    if library.prctl(option, *padded) != 0:
        raise OSError(ctypes.get_errno(), f"prctl({option}) failed")


def _apply_no_new_privileges(library: ctypes.CDLL) -> None:
    _prctl(library, _PR_SET_NO_NEW_PRIVS, 1)


def _apply_network_denial(library: ctypes.CDLL, program: bytes) -> None:
    # The filter and its descriptor must outlive the call, so both stay local.
    filters = ctypes.create_string_buffer(program, len(program))
    descriptor = ctypes.create_string_buffer(
        _SOCK_FPROG.pack(
            len(program) // _SOCK_FILTER.size, ctypes.cast(filters, ctypes.c_void_p).value
        ),
        _SOCK_FPROG.size,
    )
    # The address must cross as a pointer; a plain Python int would be passed
    # as a 32-bit argument and truncated.
    _prctl(
        library,
        _PR_SET_SECCOMP,
        _SECCOMP_MODE_FILTER,
        ctypes.c_void_p(ctypes.addressof(descriptor)),
    )


def _last_capability() -> int:
    try:
        return int(Path("/proc/sys/kernel/cap_last_cap").read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return 40


def _apply_capability_drop(library: ctypes.CDLL, privileged: bool) -> None:
    library.prctl(_PR_CAP_AMBIENT, _PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0)
    for capability in range(_last_capability() + 1):
        if library.prctl(_PR_CAPBSET_DROP, capability, 0, 0, 0) != 0 and privileged:
            # A privileged executor holds CAP_SETPCAP, so a refusal here means
            # the bounding set survives into the worker. Fail rather than lie.
            raise OSError(ctypes.get_errno(), "PR_CAPBSET_DROP failed")


def _apply_user(uid: int, gid: int) -> None:
    os.setgroups([gid])
    os.setgid(gid)
    os.setuid(uid)
    if os.getuid() != uid or os.geteuid() != uid or os.getgid() != gid:
        raise OSError(errno.EPERM, "The worker user did not take effect")


@lru_cache(maxsize=1)
def _network_denial_available() -> bool:
    """Check in a throwaway child that this kernel accepts the filter."""
    try:
        program = _seccomp_network_filter()
    except SandboxError:
        return False
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            library = _libc()
            _apply_no_new_privileges(library)
            _apply_network_denial(library, program)
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            probe.close()
            code = 1
        except OSError as error:
            code = 0 if error.errno == errno.EAFNOSUPPORT else 2
        except BaseException:
            code = 3
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status) == 0


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise SandboxError(code, message)


def worker_environment(policy: SandboxPolicy, private_tmp: Optional[Path]) -> Dict[str, str]:
    """Build the worker's whole environment from an allowlist."""
    if not policy.enabled:
        environment = dict(os.environ)
    else:
        environment = {
            name: value
            for name, value in os.environ.items()
            if name in ENVIRONMENT_ALLOWLIST or name.startswith(ENVIRONMENT_ALLOWED_PREFIXES)
        }
    if private_tmp is not None:
        # HOME moves too: anything that resolves a home directory would
        # otherwise land in the service account's, shared across jobs.
        environment["TMPDIR"] = str(private_tmp)
        environment["HOME"] = str(private_tmp)
    return environment


def _prepare_private_tmp(job_root: Path, owner: Optional[Tuple[int, int]]) -> Path:
    private_tmp = job_root / PRIVATE_TMP_NAME
    try:
        private_tmp.mkdir(mode=0o700, exist_ok=True)
        if owner is not None:
            os.chown(private_tmp, owner[0], owner[1])
    except OSError as error:
        raise SandboxError(
            "sandbox_private_tmp_unavailable",
            "The worker's private temporary directory could not be created.",
        ) from error
    return private_tmp


def _ensure_reachable(job_root: Path, uid: int, gid: int) -> None:
    """Fail before launch when the worker user cannot even reach its job.

    A state root the operator created `0700` would otherwise surface as a
    worker that exits without emitting a single event.
    """
    try:
        for ancestor in [job_root.parent, *job_root.parent.parents]:
            info = ancestor.stat()
            traversable = bool(
                info.st_mode & 0o001
                or (info.st_uid == uid and info.st_mode & 0o100)
                or (info.st_gid == gid and info.st_mode & 0o010)
            )
            if not traversable:
                raise SandboxError(
                    "sandbox_job_directory_unreachable",
                    "The worker user cannot traverse the job root's parent directories.",
                )
    except OSError as error:
        raise SandboxError(
            "sandbox_job_directory_unreachable", "The job root could not be inspected."
        ) from error


def hand_over_job_directory(job_root: Path, owner: Tuple[int, int]) -> None:
    """Give one job directory to the worker user, and nothing else with it.

    Inputs stay read-only; the directory itself becomes writable so the worker
    can publish its artifacts. The directory mode stays `0700`, so the handover
    narrows who can reach the job rather than widening it.
    """
    uid, gid = owner
    _ensure_reachable(job_root, uid, gid)
    try:
        for path in [job_root, *sorted(job_root.rglob("*"))]:
            if path.is_symlink():
                raise SandboxError(
                    "sandbox_handover_rejected", "A job directory entry is a symbolic link."
                )
            # Mode first, while the executor still owns the entry: changing it
            # afterwards would need CAP_FOWNER on top of CAP_CHOWN.
            os.chmod(path, 0o700 if path.is_dir() else 0o400)
            os.chown(path, uid, gid)
    except SandboxError:
        raise
    except OSError as error:
        raise SandboxError(
            "sandbox_handover_failed", "The job directory could not be handed to the worker user."
        ) from error


def prepare(policy: SandboxPolicy, job_root: Path) -> SandboxSetup:
    """Resolve the confinement for one launch, failing closed when it cannot hold."""
    policy.validate()
    if not policy.enabled:
        return SandboxSetup(
            environment=worker_environment(policy, None), child_setup=None, controls=()
        )

    _require(
        sys.platform == "linux",
        "sandbox_unsupported_platform",
        "The worker sandbox requires the canonical Linux container runtime.",
    )
    program = _seccomp_network_filter()
    _require(
        _network_denial_available(),
        "sandbox_network_denial_unavailable",
        "This kernel refused the worker's network filter.",
    )

    privileged = os.geteuid() == 0
    owner = policy.user
    if privileged:
        _require(
            owner is not None,
            "sandbox_privileged_executor",
            "A root executor must configure ORCA_WEB_WORKER_USER for its workers.",
        )
    elif owner is not None and owner[0] != os.geteuid():
        raise SandboxError(
            "sandbox_user_unavailable",
            "An unprivileged executor cannot switch a worker to another user.",
        )
    else:
        owner = None

    # Reaching here means the worker will not run as root either way: a
    # privileged executor was required to name a user, and an unprivileged one
    # has nobody to switch to.
    controls = ["environment", "no_new_privileges", "network", "capabilities", "user"]
    if owner is not None:
        hand_over_job_directory(job_root, owner)
    private_tmp = _prepare_private_tmp(job_root, owner) if policy.private_tmp else None
    if private_tmp is not None:
        controls.append("private_tmp")

    def child_setup() -> None:
        library = _libc()
        _apply_no_new_privileges(library)
        _apply_capability_drop(library, privileged)
        if owner is not None:
            _apply_user(*owner)
        # Last, so every step above still runs unfiltered.
        _apply_network_denial(library, program)

    return SandboxSetup(
        environment=worker_environment(policy, private_tmp),
        child_setup=child_setup,
        controls=tuple(controls),
    )
