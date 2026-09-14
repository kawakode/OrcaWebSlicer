#!/usr/bin/env python3

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import web_worker_sandbox  # noqa: E402
from web_worker_executor import ExecutorError, ExecutorLimits, execute_worker  # noqa: E402
from web_worker_sandbox import SandboxError, SandboxPolicy, policy_from_environment  # noqa: E402


# Reports its own confinement as an artifact, so every assertion below is made
# against what the kernel actually granted the worker rather than what the
# executor asked for.
REPORTING_WORKER = r'''#!/usr/bin/env python3
import errno
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

manifest = Path(sys.argv[2])
job_id = json.loads(manifest.read_text(encoding="utf-8"))["job_id"]


def status_field(name):
    for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}:"):
            return line.split(":", 1)[1].strip()
    return ""


def probe(family, kind):
    try:
        probed = socket.socket(family, kind)
        probed.close()
        return "opened"
    except OSError as error:
        return errno_name(error.errno)


def errno_name(value):
    return errno.errorcode.get(value, str(value))


report = {
    "uid": os.getuid(),
    "gid": os.getgid(),
    "no_new_privs": status_field("NoNewPrivs"),
    "seccomp": status_field("Seccomp"),
    "capability_effective": status_field("CapEff"),
    "capability_bounding": status_field("CapBnd"),
    "inet": probe(socket.AF_INET, socket.SOCK_STREAM),
    "inet6": probe(socket.AF_INET6, socket.SOCK_STREAM),
    "netlink": probe(socket.AF_NETLINK, socket.SOCK_RAW),
    "unix": probe(socket.AF_UNIX, socket.SOCK_STREAM),
    "environment": sorted(os.environ),
    "temporary_directory": tempfile.gettempdir(),
    "writable_job_directory": os.access(str(manifest.parent), os.W_OK),
}

artifact = manifest.parent / "sandbox-report.json"
artifact.write_text(json.dumps(report), encoding="utf-8")
metadata = [{
    "kind": "report",
    "path": "sandbox-report.json",
    "size_bytes": artifact.stat().st_size,
    "sha256": "unused",
}]
(manifest.parent / "result.json").write_text(
    json.dumps({
        "protocol_version": 1,
        "job_id": job_id,
        "outcome": "succeeded",
        "warnings": [],
        "timing": {"duration_ms": 1, "cpu_time_ms": 1},
        "resource_usage": {"peak_memory_bytes": 1},
        "artifacts": metadata,
        "error": None,
    }),
    encoding="utf-8",
)


def event(sequence, kind, **fields):
    value = {"protocol_version": 1, "job_id": job_id, "sequence": sequence, "type": kind}
    value.update(fields)
    print(json.dumps(value, separators=(",", ":")), flush=True)


event(0, "state", state="accepted")
event(1, "artifact", artifact=metadata[0])
event(2, "state", state="succeeded")
'''


@unittest.skipUnless(sys.platform == "linux", "the worker sandbox requires Linux")
class WorkerSandboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        # Stands in for the state root, which the API creates traversable so a
        # job directory handed to another user can still be reached.
        self.root.chmod(0o755)
        self.worker = self.root / "reporting_worker.py"
        self.worker.write_text(REPORTING_WORKER, encoding="utf-8")
        self.job = self.root / "job"
        self.job.mkdir(mode=0o700)
        (self.job / "request.json").write_text(json.dumps({"job_id": "sandbox-test"}), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def run_worker(self, policy=None):
        # A root executor must name a worker user, so the default policy under
        # test is the one that deployment would actually be allowed to use.
        if policy is None:
            policy = SandboxPolicy(user=(65534, 65534)) if os.geteuid() == 0 else SandboxPolicy()
        manifest = self.job / "request.json"
        limits = ExecutorLimits(
            wall_time_ms=20_000,
            cpu_time_ms=20_000,
            memory_bytes=512 * 1024 * 1024,
            output_bytes=1024 * 1024,
            process_count=32,
            open_files=64,
            termination_grace_ms=100,
            event_bytes=64 * 1024,
            event_line_bytes=8 * 1024,
            event_count=128,
            log_bytes=32 * 1024,
        )
        execution = execute_worker(
            [sys.executable, str(self.worker)], manifest, limits, sandbox=policy
        )
        self.assertTrue(execution.succeeded, execution.summary())
        report = json.loads((self.job / "sandbox-report.json").read_text(encoding="utf-8"))
        return execution, report

    def test_denies_every_network_address_family_that_leaves_the_host(self):
        _, report = self.run_worker()
        self.assertEqual(report["inet"], "EAFNOSUPPORT")
        self.assertEqual(report["inet6"], "EAFNOSUPPORT")
        self.assertEqual(report["netlink"], "EAFNOSUPPORT")
        # Local sockets stay available: glibc resolves users and groups over one.
        self.assertEqual(report["unix"], "opened")
        self.assertEqual(report["seccomp"], "2")

    def test_refuses_new_privileges_and_holds_no_capabilities(self):
        _, report = self.run_worker()
        self.assertEqual(report["no_new_privs"], "1")
        self.assertEqual(int(report["capability_effective"], 16), 0)

    def test_passes_only_allowlisted_environment_variables(self):
        with mock.patch.dict(
            os.environ,
            {
                "ORCA_WEB_MAX_TRIANGLES": "1000",
                "ORCA_SLICER_RESOURCES": "/workspace/resources",
                "AWS_SECRET_ACCESS_KEY": "sandbox-test-credential",
            },
        ):
            _, report = self.run_worker()
        self.assertIn("ORCA_WEB_MAX_TRIANGLES", report["environment"])
        self.assertIn("ORCA_SLICER_RESOURCES", report["environment"])
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", report["environment"])

    def test_gives_the_worker_a_temporary_directory_inside_its_own_job(self):
        _, report = self.run_worker()
        self.assertEqual(report["temporary_directory"], str(self.job / "tmp"))
        self.assertTrue((self.job / "tmp").is_dir())
        self.assertEqual((self.job / "tmp").stat().st_mode & 0o777, 0o700)
        self.assertTrue(report["writable_job_directory"])

    def test_reports_the_controls_it_applied(self):
        execution, _ = self.run_worker()
        self.assertEqual(
            set(execution.sandbox),
            {"environment", "no_new_privileges", "network", "capabilities", "user", "private_tmp"},
        )

    def test_disabled_sandbox_applies_nothing_and_says_so(self):
        with mock.patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "sandbox-test-credential"}):
            execution, report = self.run_worker(SandboxPolicy(mode="off"))
        self.assertEqual(execution.sandbox, ())
        self.assertEqual(report["inet"], "opened")
        self.assertIn("AWS_SECRET_ACCESS_KEY", report["environment"])

    @unittest.skipUnless(os.geteuid() == 0, "the user switch needs a privileged executor")
    def test_privileged_executor_hands_the_job_to_its_worker_user(self):
        policy = SandboxPolicy(user=(65534, 65534))
        execution, report = self.run_worker(policy)
        self.assertEqual((report["uid"], report["gid"]), (65534, 65534))
        self.assertEqual(int(report["capability_bounding"], 16), 0)
        self.assertTrue(report["writable_job_directory"])
        self.assertIn("user", execution.sandbox)
        handed_over = (self.job / "request.json").stat()
        self.assertEqual(handed_over.st_uid, 65534)
        self.assertEqual(handed_over.st_mode & 0o777, 0o400)

    @unittest.skipUnless(os.geteuid() == 0, "only a privileged executor can fail this way")
    def test_privileged_executor_without_a_worker_user_fails_closed(self):
        with self.assertRaises(ExecutorError) as raised:
            self.run_worker(SandboxPolicy())
        self.assertEqual(raised.exception.code, "sandbox_privileged_executor")

    @unittest.skipUnless(os.geteuid() == 0, "the reachability check guards the user switch")
    def test_rejects_a_job_root_its_worker_user_cannot_reach(self):
        self.root.chmod(0o700)
        with self.assertRaises(ExecutorError) as raised:
            self.run_worker(SandboxPolicy(user=(65534, 65534)))
        self.assertEqual(raised.exception.code, "sandbox_job_directory_unreachable")

    @unittest.skipIf(os.geteuid() == 0, "an unprivileged executor cannot switch users")
    def test_unprivileged_executor_cannot_switch_to_another_user(self):
        with self.assertRaises(ExecutorError) as raised:
            self.run_worker(SandboxPolicy(user=(os.geteuid() + 1, os.getegid() + 1)))
        self.assertEqual(raised.exception.code, "sandbox_user_unavailable")

    def test_rejects_a_worker_user_that_would_keep_root(self):
        with self.assertRaises(SandboxError) as raised:
            SandboxPolicy(user=(0, 0)).validate()
        self.assertEqual(raised.exception.code, "invalid_sandbox_user")

    def test_reads_its_policy_from_the_environment(self):
        with mock.patch.dict(
            os.environ, {"ORCA_WEB_WORKER_SANDBOX": "off", "ORCA_WEB_WORKER_USER": "65534"}
        ):
            policy = policy_from_environment()
        self.assertEqual(policy.mode, "off")
        self.assertEqual(policy.user, (65534, 65534))

        with mock.patch.dict(os.environ, {"ORCA_WEB_WORKER_SANDBOX": "maybe"}):
            with self.assertRaises(SandboxError) as raised:
                policy_from_environment()
        self.assertEqual(raised.exception.code, "invalid_sandbox_mode")

        with mock.patch.dict(os.environ, {"ORCA_WEB_WORKER_USER": "nobody"}):
            with self.assertRaises(SandboxError) as raised:
                policy_from_environment()
        self.assertEqual(raised.exception.code, "invalid_sandbox_user")

    def test_required_sandbox_fails_closed_when_the_kernel_refuses_the_filter(self):
        web_worker_sandbox._network_denial_available.cache_clear()
        self.addCleanup(web_worker_sandbox._network_denial_available.cache_clear)
        with mock.patch.object(
            web_worker_sandbox, "_network_denial_available", return_value=False
        ):
            with self.assertRaises(SandboxError) as raised:
                web_worker_sandbox.prepare(SandboxPolicy(), self.job)
        self.assertEqual(raised.exception.code, "sandbox_network_denial_unavailable")

    def test_refuses_to_hand_a_job_directory_over_through_a_symbolic_link(self):
        (self.job / "escape").symlink_to(self.root)
        with self.assertRaises(SandboxError) as raised:
            web_worker_sandbox.hand_over_job_directory(self.job, (65534, 65534))
        self.assertEqual(raised.exception.code, "sandbox_handover_rejected")


if __name__ == "__main__":
    unittest.main()
