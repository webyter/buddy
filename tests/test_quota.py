"""Quota tracker tests: rate-limit header harvest, 429 quota-body parsing,
and the /quota report — buddy has no quota API to ask, so everything here
rides on what the API tells it in flight."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest import mock  # noqa: E402


class TestQuota(unittest.TestCase):
    def setUp(self):
        import buddy_core.quota as q
        self.q = q
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        p = Path(td.name) / "quota.json"
        patcher = mock.patch.object(q, "QUOTA_FILE", p)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_record_headers_harvests_and_ignores(self):
        self.q.record_headers("https://g.api/v1", "gem-1", {
            "X-RateLimit-Remaining-Requests": "91",
            "x-ratelimit-remaining-tokens": "241000",
            "content-type": "application/json",  # not a rate-limit header
        })
        data = json.loads(self.q.QUOTA_FILE.read_text())
        entry = data["https://g.api/v1|gem-1"]
        self.assertEqual(entry["headers"]["x-ratelimit-remaining-requests"], "91")
        self.assertEqual(entry["headers"]["x-ratelimit-remaining-tokens"], "241000")
        self.assertNotIn("content-type", entry["headers"])

    def test_record_429_parses_google_quota_body(self):
        # Google's OpenAI-compat endpoint wraps the error object in an array —
        # a bare .get("error") on that throws and the quota info is lost.
        body = json.dumps([{"error": {
            "code": 429, "status": "RESOURCE_EXHAUSTED",
            "message": "Quota exceeded",
            "details": [
                {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                 "violations": [{"quotaId": "GenerateRequestsPerDayPerProject",
                                 "quotaValue": "20"}]},
                {"@type": "type.googleapis.com/google.rpc.RetryInfo",
                 "retryDelay": "23s"},
            ]}}])
        self.q.record_429("https://g.api/v1", "gem-1", body)
        data = json.loads(self.q.QUOTA_FILE.read_text())
        entry = data["https://g.api/v1|gem-1"]
        self.assertIn("PerDay", entry["daily_exhausted"])
        self.assertEqual(entry["retry_after"], "23s")
        self.assertEqual(entry["bucket_size"], "20")

    def test_record_429_survives_non_json_body(self):
        self.q.record_429("https://g.api/v1", "gem-1", "<html>slow down</html>")
        data = json.loads(self.q.QUOTA_FILE.read_text())
        self.assertIn("https://g.api/v1|gem-1", data)

    def test_report_empty_and_populated(self):
        self.assertTrue(self.q.report({}).startswith("(no quota data"))
        self.q.record_headers("https://g.api/v1", "gem-1", {
            "x-ratelimit-remaining-requests": "91"})
        out = self.q.report({})
        self.assertIn("gem-1", out)
        self.assertIn("x-ratelimit-remaining-requests: 91", out)

    def test_daily_exhaustion_shown_in_report(self):
        self.q.record_429("https://g.api/v1", "gem-1", json.dumps({"error": {
            "code": 429, "status": "RESOURCE_EXHAUSTED",
            "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                         "violations": [{"quotaId": "GenerateRequestsPerDay"}]}]}}))
        self.assertIn("DAILY QUOTA EXHAUSTED", self.q.report({}))

    def test_slash_command_renders_report(self):
        from buddy_core.commands import run_slash_command
        self.q.record_headers("https://g.api/v1", "gem-1", {
            "x-ratelimit-remaining-tokens": "1000"})
        out = run_slash_command({}, "/quota")
        self.assertIn("x-ratelimit-remaining-tokens: 1000", out)


if __name__ == "__main__":
    unittest.main()
