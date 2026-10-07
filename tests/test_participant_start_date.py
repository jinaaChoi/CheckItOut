import unittest
from datetime import date
from unittest.mock import patch

import bot


class ParticipantStartDateTests(unittest.TestCase):
    def cfg_value(self, key):
        values = {
            "challenge_days": [0, 1, 2, 3, 4],
            "fine_late": 1000,
            "fine_absent": 2000,
            "challenge_topic": "크로키",
            "global_rest_periods": [],
        }
        return values.get(key)

    def test_dates_before_joining_are_excluded_from_fines(self):
        week_dates = [date(2026, 10, 5) + bot.timedelta(days=i) for i in range(5)]
        counts = {d: 0 for d in week_dates}
        counts[date(2026, 10, 7)] = 1
        inactive_dates = {date(2026, 10, 5), date(2026, 10, 6)}

        with patch.object(bot.cfg, "get", side_effect=self.cfg_value):
            status = bot.judge_member_week(
                week_dates, counts, [], [], {}, set(), inactive_dates
            )
            late, absent, amount = bot.count_fine_from_status(status)

        self.assertEqual(status[date(2026, 10, 5)], "참여전")
        self.assertEqual(status[date(2026, 10, 6)], "참여전")
        self.assertEqual(status[date(2026, 10, 7)], "정상")
        self.assertEqual((late, absent, amount), (0, 2, 4000))

    def test_weekly_report_marks_pre_join_dates_with_dash(self):
        week_dates = [date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)]
        status = {
            week_dates[0]: "참여전",
            week_dates[1]: "참여전",
            week_dates[2]: "정상",
        }

        with patch.object(bot.cfg, "get", side_effect=self.cfg_value):
            embed = bot.build_weekly_report({"신규": status}, week_dates).to_dict()

        self.assertIn("월➖", embed["fields"][0]["name"])
        self.assertIn("참여 전 2일 제외", embed["fields"][0]["value"])


if __name__ == "__main__":
    unittest.main()
