import tempfile
import unittest
from pathlib import Path

from crm import Store


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.store = Store(self.db)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_project_requires_name(self) -> None:
        with self.assertRaises(ValueError):
            self.store.add_project("  ")

    def test_log_entry_needs_a_project(self) -> None:
        with self.assertRaises(ValueError):
            self.store.add_entry(999, "call", "Talked to Ada")

    def test_status_and_log_feed_a_report(self) -> None:
        pid = self.store.add_project("Family laptop", client="Ada")
        self.store.set_status(pid, "Live USB passed. Waiting on backup disk.")
        self.store.add_entry(
            pid, "call", "Ada confirmed she has a spare 32GB stick.", who="Ada"
        )
        self.store.add_entry(
            pid, "step", "Booted Mint live USB; Wi-Fi and print both worked."
        )
        self.store.add_entry(pid, "blocker", "No backup disk yet. Will not wipe.")
        report = self.store.make_report(pid)
        self.assertIn("Live USB passed", report["status"])
        bodies = [e["body"] for e in report["entries"]]
        self.assertTrue(any("32GB stick" in b for b in bodies))
        self.assertTrue(any("Will not wipe" in b for b in bodies))
        self.assertEqual(report["project_name"], "Family laptop")

    def test_report_can_cut_off_old_entries(self) -> None:
        pid = self.store.add_project("Shop PC")
        self.store.add_entry(pid, "note", "First contact", happened_at="2026-01-01T10:00:00+00:00")
        self.store.add_entry(pid, "step", "Installed Mint", happened_at="2026-08-01T10:00:00+00:00")
        report = self.store.make_report(pid, since="2026-06-01T00:00:00+00:00")
        bodies = [e["body"] for e in report["entries"]]
        self.assertNotIn("First contact", bodies)
        self.assertIn("Installed Mint", bodies)

    def test_open_blockers_listed_on_project(self) -> None:
        pid = self.store.add_project("Printer")
        self.store.add_entry(pid, "blocker", "Needs USB cable")
        self.store.add_entry(pid, "step", "Ordered cable")
        blockers = self.store.open_blockers(pid)
        self.assertEqual(len(blockers), 1)
        self.assertIn("USB cable", blockers[0]["body"])

    def test_board_groups_projects_by_lane(self) -> None:
        self.store.add_project("Live USB", lane="Planning")
        self.store.add_project("Mint install", lane="Execution", health="stuck")
        board = self.store.board()
        self.assertEqual([p["name"] for p in board["Planning"]], ["Live USB"])
        self.assertEqual([p["name"] for p in board["Execution"]], ["Mint install"])
        self.assertEqual(board["Execution"][0]["health"], "stuck")
        self.assertEqual(list(board), ["Planning", "Execution", "Launch"])

    def test_dev_column_inserts_after_client(self) -> None:
        pid = self.store.add_project("Family laptop", client="Ada")
        col_id = self.store.add_column("Assigned", after="client")
        keys = [c["key"] for c in self.store.list_columns()]
        self.assertEqual(keys[:4], ["name", "client", "assigned", "health"])
        self.store.set_cell(pid, col_id, "Jordan")
        self.assertEqual(self.store.cell_value(pid, col_id), "Jordan")
        self.store.set_dev_mode(True)
        self.assertTrue(self.store.dev_mode())


if __name__ == "__main__":
    unittest.main()
