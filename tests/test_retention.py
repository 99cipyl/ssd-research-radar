import datetime as dt
import unittest

import retention


class RetentionPolicyTests(unittest.TestCase):
    def test_rolling_cutoff_uses_exact_calendar_years(self):
        self.assertEqual(
            retention.history_cutoff(
                {"history_window_years": 5, "history_start_date": "2000-01-01"},
                today=dt.date(2026, 7, 20),
            ),
            "2021-07-20",
        )

    def test_leap_day_clamps_and_configured_floor_can_be_stricter(self):
        self.assertEqual(
            retention.history_cutoff(
                {"history_window_years": 1}, today=dt.date(2024, 2, 29)
            ),
            "2023-02-28",
        )
        self.assertEqual(
            retention.history_cutoff(
                {"history_window_years": 5, "history_start_date": "2024-01-01"},
                today=dt.date(2026, 7, 20),
            ),
            "2024-01-01",
        )

    def test_window_size_is_bounded(self):
        for value in (0, 21):
            with self.assertRaises(ValueError):
                retention.history_window_years({"history_window_years": value})

    def test_historical_records_use_publication_date(self):
        cutoff = "2021-07-20"
        self.assertFalse(
            retention.record_is_in_scope({"published_at": "2021-07-19"}, cutoff)
        )
        self.assertTrue(
            retention.record_is_in_scope({"published_at": "2021-07-20"}, cutoff)
        )
        # Undated current announcements may still be tracked as live events,
        # but SQL history queries exclude them until a date is known.
        self.assertTrue(retention.record_is_in_scope({"published_at": None}, cutoff))

    def test_current_updates_to_old_material_remain_eligible(self):
        cutoff = "2021-07-20"
        today = "2026-07-20T12:00:00Z"
        self.assertTrue(
            retention.event_is_in_scope("updated", "2018-01-01", today, cutoff)
        )
        self.assertFalse(
            retention.event_is_in_scope("new", "2018-01-01", today, cutoff)
        )
        self.assertTrue(retention.event_is_in_scope("new", None, today, cutoff))
        self.assertFalse(
            retention.event_is_in_scope(
                "updated", "2026-01-01", "2021-07-19T23:59:59Z", cutoff
            )
        )


if __name__ == "__main__":
    unittest.main()
