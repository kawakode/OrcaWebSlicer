#!/usr/bin/env python3

import io
import json
import logging
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from web_structured_log import FORMAT_JSON, FORMAT_TEXT, JsonFormatter, TextFormatter, configure, log_event  # noqa: E402


class StructuredLogTests(unittest.TestCase):
    def capture(self, formatter):
        logger = logging.getLogger("orca.web.test-structured")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        self.addCleanup(logger.removeHandler, handler)
        return logger, stream

    def test_writes_one_json_object_per_record(self):
        logger, stream = self.capture(JsonFormatter())
        log_event(logger, logging.INFO, "job.accepted", job_id="abc", staged_bytes=12, retry_of=None)
        log_event(logger, logging.WARNING, "worker.exited", job_id="abc", status="timed_out")

        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first["event"], "job.accepted")
        self.assertEqual(first["level"], "info")
        self.assertEqual(first["logger"], "orca.web.test-structured")
        self.assertEqual(first["job_id"], "abc")
        self.assertEqual(first["staged_bytes"], 12)
        self.assertRegex(first["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        # An absent value is left out rather than written as null.
        self.assertNotIn("retry_of", first)
        self.assertEqual(json.loads(lines[1])["level"], "warning")

    def test_a_field_cannot_overwrite_the_envelope(self):
        logger, stream = self.capture(JsonFormatter())
        log_event(logger, logging.INFO, "real", level="critical", ts="forged", logger="other")
        document = json.loads(stream.getvalue())
        self.assertEqual((document["event"], document["level"], document["logger"]),
                         ("real", "info", "orca.web.test-structured"))
        self.assertNotEqual(document["ts"], "forged")

    def test_formats_plain_records_and_exceptions(self):
        logger, stream = self.capture(JsonFormatter())
        logger.info("started on %s", "port")
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("job.raised", extra={"fields": {"job_id": "abc"}})
        # The traceback is escaped inside the record, so it is still one line.
        plain, failure = (json.loads(line) for line in stream.getvalue().splitlines())
        self.assertEqual(plain["event"], "started on port")
        self.assertEqual(failure["job_id"], "abc")
        self.assertIn("ValueError: boom", failure["exception"])

    def test_text_format_renders_fields_as_pairs(self):
        logger, stream = self.capture(TextFormatter())
        log_event(logger, logging.INFO, "deleted", kind="upload", bytes=3)
        self.assertEqual(stream.getvalue().strip(), "INFO orca.web.test-structured deleted kind=upload bytes=3")

    def test_configure_is_idempotent_and_refuses_unknown_formats(self):
        name = "orca.web.test-configure"
        self.addCleanup(lambda: logging.getLogger(name).handlers.clear())
        configure(FORMAT_JSON, loggers=(name,))
        configure(FORMAT_TEXT, loggers=(name,))
        logger = logging.getLogger(name)
        self.assertEqual(len(logger.handlers), 1)
        self.assertIsInstance(logger.handlers[0].formatter, TextFormatter)
        self.assertFalse(logger.propagate)
        with self.assertRaises(ValueError):
            configure("xml", loggers=(name,))


if __name__ == "__main__":
    unittest.main()
