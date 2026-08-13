from datetime import datetime, timezone
import os
import unittest
from unittest.mock import patch

import daily_youtube_metadata_update as job


def check(raw_status: str, at: str) -> dict:
    return job.make_availability_check(
        "run-test",
        "video-test",
        raw_status,
        datetime.fromisoformat(at).replace(tzinfo=timezone.utc),
    )


class AvailabilityStateTest(unittest.TestCase):
    def test_primary_and_fallback_api_keys_are_loaded(self):
        with patch.dict(
            os.environ,
            {
                "YOUTUBE_API_KEY_PRIMARY": "primary-key",
                "YOUTUBE_API_KEY_FALLBACK": "fallback-key",
                "YOUTUBE_API_KEY": "",
                "YOUTUBE_API_KEYS": "",
            },
            clear=False,
        ):
            self.assertEqual(
                [("primary", "primary-key"), ("fallback", "fallback-key")],
                job.load_api_keys(),
            )

    def test_new_public_record_is_baseline_without_event(self):
        state, event = job.derive_availability_state(None, check("public", "2026-08-13T00:00:00"))
        self.assertEqual("public", state["status"])
        self.assertEqual(0, state["consecutive_missing_count"])
        self.assertIsNone(event)

    def test_first_missing_is_suspected(self):
        previous = {
            "status": "public",
            "last_seen_public_at": "2026-08-12T00:00:00+00:00",
            "consecutive_missing_count": 0,
        }
        state, event = job.derive_availability_state(previous, check("missing_api", "2026-08-13T00:00:00"))
        self.assertEqual("suspected_unavailable", state["status"])
        self.assertEqual(1, state["consecutive_missing_count"])
        self.assertEqual("suspected_unavailable", event["new_status"])

    def test_second_missing_is_confirmed(self):
        previous = {
            "status": "suspected_unavailable",
            "last_seen_public_at": "2026-08-12T00:00:00+00:00",
            "first_missing_at": "2026-08-13T00:00:00+00:00",
            "consecutive_missing_count": 1,
        }
        state, event = job.derive_availability_state(previous, check("missing_api", "2026-08-13T03:00:00"))
        self.assertEqual("confirmed_unavailable", state["status"])
        self.assertEqual(2, state["consecutive_missing_count"])
        self.assertEqual("confirmed_unavailable", event["new_status"])

    def test_restored_public_resets_missing_state(self):
        previous = {
            "status": "confirmed_unavailable",
            "last_seen_public_at": "2026-08-12T00:00:00+00:00",
            "first_missing_at": "2026-08-13T00:00:00+00:00",
            "confirmed_unavailable_at": "2026-08-13T03:00:00+00:00",
            "consecutive_missing_count": 2,
        }
        state, event = job.derive_availability_state(previous, check("public", "2026-08-13T06:00:00"))
        self.assertEqual("public", state["status"])
        self.assertEqual(0, state["consecutive_missing_count"])
        self.assertIsNone(state["first_missing_at"])
        self.assertEqual("public", event["new_status"])


if __name__ == "__main__":
    unittest.main()
