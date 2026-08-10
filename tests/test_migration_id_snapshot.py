import unittest

from scripts.migration_id_snapshot import _compare


class MigrationSnapshotTests(unittest.TestCase):
    def test_compare_accepts_new_empty_migration_tables(self):
        before = {
            "tables": {"LiteLLM_UserTable.user_id": {"present": True, "count": 2, "sha256": "same"}},
            "spend_history_row_counts": {"LiteLLM_SpendLogs": 7},
        }
        after = {
            "tables": {"LiteLLM_UserTable.user_id": {"present": True, "count": 2, "sha256": "same"}},
            "spend_history_row_counts": {"LiteLLM_SpendLogs": 7, "LiteLLM_NewSpendTable": 0},
            "foreign_keys": {"unvalidated": []},
        }
        self.assertEqual(_compare(before, after), [])

    def test_compare_fails_on_identifier_spend_or_constraint_regression(self):
        before = {
            "tables": {"gateway_generation_jobs.id": {"present": True, "count": 2, "sha256": "before"}},
            "spend_history_row_counts": {"LiteLLM_SpendLogs": 7},
        }
        after = {
            "tables": {"gateway_generation_jobs.id": {"present": True, "count": 2, "sha256": "after"}},
            "spend_history_row_counts": {"LiteLLM_SpendLogs": 6},
            "foreign_keys": {"unvalidated": ["child.parent_fk"]},
        }
        self.assertEqual(
            _compare(before, after),
            [
                "gateway_generation_jobs.id",
                "spend_history_row_counts.LiteLLM_SpendLogs",
                "foreign_keys.unvalidated",
            ],
        )


if __name__ == "__main__":
    unittest.main()
