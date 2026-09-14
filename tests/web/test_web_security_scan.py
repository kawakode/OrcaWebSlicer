#!/usr/bin/env python3

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import web_security_scan  # noqa: E402


def write_sbom(path: Path) -> None:
    path.write_text(
        json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.6", "components": []}),
        encoding="utf-8",
    )


def write_report(path: Path) -> None:
    path.write_text(json.dumps({"matches": [], "source": {}, "descriptor": {}}), encoding="utf-8")


class RecordingRunner:
    def __init__(self, policy_codes=(0, 0)):
        self.commands = []
        self.policy_codes = iter(policy_codes)

    def __call__(self, command):
        command = list(command)
        self.commands.append(command)
        if command[0] == "test-syft":
            output = Path(command[command.index("--output") + 1].split("=", 1)[1])
            write_sbom(output)
            return subprocess.CompletedProcess(command, 0)
        if "--file" in command:
            write_report(Path(command[command.index("--file") + 1]))
            return subprocess.CompletedProcess(command, 0)
        return subprocess.CompletedProcess(command, next(self.policy_codes))


class CommandTests(unittest.TestCase):
    def test_builds_explicit_sources_and_separate_report_and_policy_commands(self):
        sbom = Path("out/image.sbom.cdx.json")
        report = Path("out/image.grype.json")
        self.assertEqual(
            web_security_scan.syft_command(
                "dir:/", sbom, source_name="example:tag", excludes=("./source/**",)
            ),
            [
                "syft",
                "dir:/",
                "--output",
                f"cyclonedx-json={sbom}",
                "--source-name",
                "example:tag",
                "--exclude",
                "./source/**",
            ],
        )
        self.assertEqual(
            web_security_scan.grype_report_command(sbom, report),
            ["grype", f"sbom:{sbom}", "--output", "json", "--file", str(report)],
        )
        self.assertEqual(
            web_security_scan.grype_policy_command(sbom),
            ["grype", f"sbom:{sbom}", "--only-fixed", "--fail-on", "high", "--output", "table"],
        )


class ArtifactValidationTests(unittest.TestCase):
    def test_rejects_non_cyclonedx_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sbom.json"
            path.write_text('{"bomFormat":"SPDX","components":[]}', encoding="utf-8")
            with self.assertRaises(web_security_scan.ScanError):
                web_security_scan.validate_cyclonedx(path)

    def test_rejects_malformed_grype_output(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text('{"matches":{}}', encoding="utf-8")
            with self.assertRaises(web_security_scan.ScanError):
                web_security_scan.validate_grype_report(path)


class OrchestrationTests(unittest.TestCase):
    def test_scans_both_targets_and_retains_complete_reports_before_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = RecordingRunner()
            failures = web_security_scan.run_scan(
                image="example:tag",
                frontend=Path("/source/web/frontend"),
                output_dir=Path(directory),
                syft="test-syft",
                grype="test-grype",
                runner=runner,
            )
        self.assertEqual(failures, [])
        self.assertEqual(len(runner.commands), 6)
        self.assertEqual(runner.commands[0][1], "dir:/")
        self.assertIn("example:tag", runner.commands[0])
        self.assertIn("./source/**", runner.commands[0])
        self.assertEqual(runner.commands[1][1], "dir:/source/web/frontend")
        self.assertEqual(runner.commands[1][-2:], ["--source-name", "orca-web-frontend"])
        self.assertIn("--file", runner.commands[2])
        self.assertNotIn("--only-fixed", runner.commands[2])
        self.assertIn("--only-fixed", runner.commands[4])

    def test_classifies_grype_exit_two_as_a_policy_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            failures = web_security_scan.run_scan(
                image="example:tag",
                frontend=Path("/source/web/frontend"),
                output_dir=Path(directory),
                syft="test-syft",
                grype="test-grype",
                runner=RecordingRunner(policy_codes=(2, 0)),
            )
        self.assertEqual(failures, ["image"])

    def test_treats_other_nonzero_status_as_scanner_failure(self):
        def failing_runner(command):
            return subprocess.CompletedProcess(command, 17)

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(web_security_scan.ScanError):
                web_security_scan.run_scan(
                    image="example:tag",
                    frontend=Path("/source/web/frontend"),
                    output_dir=Path(directory),
                    runner=failing_runner,
                )


if __name__ == "__main__":
    unittest.main()
