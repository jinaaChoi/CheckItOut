import unittest
from unittest.mock import patch

import bot
import store


class MemberRenameTests(unittest.TestCase):
    def test_rename_moves_all_name_keyed_records(self):
        attendance = {
            "2026-09-01": {"짹": {"count": 1}},
            "2026-09-02": {"9987": {"count": 2}},
        }
        fines = {"2026-08-31": {"짹": {"amount": 2000, "paid": False}}}
        overrides = {"2026-09-03": {"짹": "휴식"}}

        with (
            patch.object(store, "_attendance", attendance),
            patch.object(store, "_fines", fines),
            patch.object(store, "_overrides", overrides),
            patch.object(store, "_save", return_value=True),
        ):
            result = store.rename_member("짹", "9987")

        self.assertTrue(result["renamed"])
        self.assertEqual(result["moved"], {"attendance": 1, "fines": 1, "overrides": 1})
        self.assertNotIn("짹", attendance["2026-09-01"])
        self.assertEqual(attendance["2026-09-01"]["9987"], {"count": 1})
        self.assertEqual(fines["2026-08-31"]["9987"]["amount"], 2000)
        self.assertEqual(overrides["2026-09-03"]["9987"], "휴식")

    def test_rename_refuses_different_records_in_same_bucket(self):
        fines = {
            "2026-08-31": {
                "짹": {"amount": 2000},
                "9987": {"amount": 4000},
            }
        }
        with (
            patch.object(store, "_attendance", {}),
            patch.object(store, "_fines", fines),
            patch.object(store, "_overrides", {}),
            patch.object(store, "_save", return_value=True),
        ):
            result = store.rename_member("짹", "9987")

        self.assertFalse(result["renamed"])
        self.assertEqual(result["conflicts"], ["벌금:2026-08-31"])
        self.assertIn("짹", fines["2026-08-31"])

    def test_registration_removes_stale_mapping_and_migrates(self):
        mappings = {
            "크로키-짹": 1234,
            "크로키-9987": 1234,
        }
        rename_result = {
            "renamed": True,
            "moved": {"attendance": 0, "fines": 1, "overrides": 0},
            "conflicts": [],
        }
        with (
            patch.object(bot.cfg, "get", return_value="크로키-"),
            patch.object(store, "get_member_rename_conflicts", return_value=[]),
            patch.object(store, "rename_member", return_value=rename_result) as rename,
        ):
            result = bot.sync_member_registration(mappings, "크로키-9987", 1234)

        self.assertTrue(result["ok"])
        self.assertEqual(result["old_names"], ["짹"])
        self.assertNotIn("크로키-짹", result["channel_members"])
        self.assertEqual(result["channel_members"]["크로키-9987"], 1234)
        rename.assert_called_once_with("짹", "9987")

    def test_registration_checks_conflicts_between_multiple_old_names(self):
        mappings = {
            "크로키-첫이름": 1234,
            "크로키-둘째이름": 1234,
        }

        def conflicts(left, right):
            if {left, right} == {"첫이름", "둘째이름"}:
                return ["벌금:2026-08-31"]
            return []

        with (
            patch.object(bot.cfg, "get", return_value="크로키-"),
            patch.object(store, "get_member_rename_conflicts", side_effect=conflicts),
            patch.object(store, "rename_member") as rename,
        ):
            result = bot.sync_member_registration(mappings, "크로키-9987", 1234)

        self.assertFalse(result["ok"])
        self.assertEqual(result["conflicts"], ["벌금:2026-08-31"])
        rename.assert_not_called()


if __name__ == "__main__":
    unittest.main()
