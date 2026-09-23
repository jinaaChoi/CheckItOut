import unittest
from datetime import date
from unittest.mock import patch

import bot


class GlobalRestTests(unittest.TestCase):
    def setUp(self):
        self.periods = [{
            "id": 1,
            "start": "2026-09-22",
            "end": "2026-09-24",
            "reason": "추석 연휴",
        }]

    def cfg_value(self, key):
        values = {
            "global_rest_periods": self.periods,
            "challenge_days": [0, 1, 2, 3, 4],
            "fine_late": 1000,
            "fine_absent": 2000,
            "challenge_topic": "크로키",
        }
        return values.get(key)

    def test_weekends_and_global_rest_do_not_publish_automatic_report(self):
        with patch.object(bot.cfg, "get", side_effect=self.cfg_value):
            self.assertFalse(bot.should_publish_automatic_report(date(2026, 9, 20)))  # 일
            self.assertFalse(bot.should_publish_automatic_report(date(2026, 9, 22)))  # 화, 전체 휴식
            self.assertTrue(bot.should_publish_automatic_report(date(2026, 9, 21)))   # 월

    def test_global_rest_overrides_manual_status_and_has_no_fine(self):
        week_dates = [date(2026, 9, 21) + bot.timedelta(days=i) for i in range(5)]
        counts = {d: 0 for d in week_dates}
        overrides = {date(2026, 9, 22): "결석"}
        global_rest_dates = {date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24)}

        with patch.object(bot.cfg, "get", side_effect=self.cfg_value):
            status = bot.judge_member_week(
                week_dates, counts, [], [], overrides, global_rest_dates
            )
            self.assertEqual(status[date(2026, 9, 22)], "전체휴식")
            self.assertEqual(status[date(2026, 9, 23)], "전체휴식")
            self.assertEqual(status[date(2026, 9, 24)], "전체휴식")
            late, absent, amount = bot.count_fine_from_status(status)
            self.assertEqual((late, absent, amount), (0, 2, 4000))

    def test_post_holiday_upload_does_not_rescue_pre_holiday_absence(self):
        week_dates = [date(2026, 9, 21) + bot.timedelta(days=i) for i in range(5)]
        counts = {d: 0 for d in week_dates}
        counts[date(2026, 9, 25)] = 2
        global_rest_dates = {date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24)}

        with patch.object(bot.cfg, "get", side_effect=self.cfg_value):
            status = bot.judge_member_week(
                week_dates, counts, [], [], {}, global_rest_dates
            )
            self.assertEqual(status[date(2026, 9, 21)], "결석")
            self.assertEqual(status[date(2026, 9, 25)], "정상")

    def test_weekly_report_uses_music_icon(self):
        week_dates = [date(2026, 9, 21) + bot.timedelta(days=i) for i in range(5)]
        status = {
            week_dates[0]: "정상",
            week_dates[1]: "전체휴식",
            week_dates[2]: "전체휴식",
            week_dates[3]: "전체휴식",
            week_dates[4]: "정상",
        }

        with patch.object(bot.cfg, "get", side_effect=self.cfg_value):
            embed = bot.build_weekly_report({"테스트": status}, week_dates)
            data = embed.to_dict()
            self.assertIn("화🎵", data["fields"][0]["name"])
            self.assertIn("추석 연휴", data["description"])


if __name__ == "__main__":
    unittest.main()
