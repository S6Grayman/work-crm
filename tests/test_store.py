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

    def test_column_insert_and_rename(self) -> None:
        pid = self.store.add_project("Family laptop", client="Ada")
        col_id = self.store.add_column("Assigned", after="client")
        keys = [c["key"] for c in self.store.list_columns()]
        self.assertEqual(keys[:4], ["name", "client", "assigned", "health"])
        self.store.set_cell(pid, col_id, "Jordan")
        self.assertEqual(self.store.cell_value(pid, col_id), "Jordan")
        self.store.rename_column(col_id, "Owner")
        labels = [c["label"] for c in self.store.list_columns()]
        self.assertIn("Owner", labels)
        self.store.insert_column("Shop", beside="client", side="left")
        keys = [c["key"] for c in self.store.list_columns()]
        self.assertEqual(keys[:4], ["name", "shop", "client", "assigned"])

    def test_project_matches_column_filter(self) -> None:
        stuck = self.store.add_project("A", client="Ada", health="stuck")
        self.store.add_project("B", client="Bob", health="working")
        a = self.store.get_project(stuck)
        self.assertTrue(self.store.matches(a, {"health": "stuck"}))
        self.assertFalse(self.store.matches(a, {"health": "working"}))
        self.assertTrue(self.store.matches(a, {"client": "Ada"}))

    def test_client_profile_required_and_linked_to_project(self) -> None:
        with self.assertRaises(ValueError):
            self.store.add_client("  ")
        cid = self.store.add_client(
            "Ada Household",
            contact_name="Ada",
            phone="555-0100",
            email="ada@example.com",
            city="Martinsville",
        )
        client = self.store.get_client(cid)
        self.assertEqual(client["name"], "Ada Household")
        self.assertEqual(client["contact_name"], "Ada")
        pid = self.store.add_project("Mint install", client_id=cid)
        job = self.store.get_project(pid)
        self.assertEqual(job["client_id"], cid)
        self.assertEqual(job["client"], "Ada Household")
        linked = self.store.projects_for_client(cid)
        self.assertEqual(len(linked), 1)
        self.assertEqual(linked[0]["id"], pid)

    def test_project_attachments_round_trip(self) -> None:
        pid = self.store.add_project("Family laptop")
        aid = self.store.add_attachment(pid, "quote.pdf", b"%PDF-fake", "application/pdf")
        rows = self.store.list_attachments(pid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "quote.pdf")
        data, mime, name = self.store.read_attachment(pid, aid)
        self.assertEqual(data, b"%PDF-fake")
        self.assertEqual(name, "quote.pdf")
        self.assertIn("pdf", mime)
        self.store.delete_attachment(pid, aid)
        self.assertEqual(self.store.list_attachments(pid), [])
        with self.assertRaises(ValueError):
            self.store.add_attachment(pid, "evil.exe", b"MZ", "application/octet-stream")
        with self.assertRaises(ValueError):
            self.store.add_attachment(999, "x.pdf", b"x", "application/pdf")

    def test_archive_hides_from_board(self) -> None:
        live = self.store.add_project("Live USB", lane="Planning")
        done = self.store.add_project("Old job", lane="Launch", health="done")
        self.store.set_archived(done, True)
        board = self.store.board()
        names = [p["name"] for lane in board.values() for p in lane]
        self.assertIn("Live USB", names)
        self.assertNotIn("Old job", names)
        archived = self.store.list_archived()
        self.assertEqual([p["name"] for p in archived], ["Old job"])
        self.store.set_archived(done, False)
        names = [p["name"] for lane in self.store.board().values() for p in lane]
        self.assertIn("Old job", names)
        self.assertEqual(self.store.list_archived(), [])
        self.assertEqual(self.store.get_project(live)["archived"], 0)

    def test_merge_import_keeps_local_work(self) -> None:
        self.store.set_profile("ada@example.com", "Ada")
        self.store.add_project("Ada job")
        pkg = self.store.export_package()
        other_db = Path(self.tmp.name) / "other.db"
        other = Store(other_db)
        other.set_profile("bob@example.com", "Bob")
        other.add_project("Bob only")
        summary = other.import_package(pkg)
        names = {p["name"] for lane in other.board().values() for p in lane}
        self.assertIn("Bob only", names)
        self.assertIn("Ada job", names)
        ada = next(p for p in other.list_projects() if p["name"] == "Ada job")
        self.assertEqual(ada["created_by"], "ada@example.com")
        self.assertEqual(ada["last_edited_by"], "ada@example.com")
        self.assertGreaterEqual(summary["added"], 1)
        other.close()

    def test_export_writes_to_chosen_folder(self) -> None:
        self.store.set_profile("ada@example.com", "Ada")
        dest = Path(self.tmp.name) / "exports"
        self.store.set_setting("export_dir", str(dest))
        path = self.store.write_export()
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent, dest.resolve())
        self.assertTrue(path.name.endswith(".workcrm"))


if __name__ == "__main__":
    unittest.main()
