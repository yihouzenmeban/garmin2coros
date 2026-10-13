from pathlib import Path
import tempfile
from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch

from scripts.garmin import garmin_sync_coros
from garmin import garmin_db
import sqlite_db


class GarminDBRetryTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.database_dir = Path(directory.name)
        for module in (garmin_db, sqlite_db):
            patcher = patch.object(module, "DB_DIR", str(self.database_dir))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.database = garmin_sync_coros.GarminDB("garmin.db")
        self.database.initDB()

    def test_pending_query_includes_failed_records_and_skips_synced_records(self):
        for activity_id in (11, 22, 33):
            self.database.saveActivity(activity_id)
        self.database.updateSyncStatus(22)
        self.database.updateExceptionSyncStatus(33)

        self.assertEqual(self.database.getUnSyncActivity(), [11, 33])

    def test_only_failed_records_are_still_pending(self):
        self.database.saveActivity(11)
        self.database.updateExceptionSyncStatus(11)

        self.assertEqual(self.database.getUnSyncActivity(), [11])

    def test_successful_retry_stops_the_record_from_being_selected_again(self):
        self.database.saveActivity(11)
        self.database.updateExceptionSyncStatus(11)
        self.database.updateSyncStatus(11)

        self.assertIsNone(self.database.getUnSyncActivity())

    def test_batch_insert_deduplicates_and_preserves_existing_status(self):
        self.database.saveActivity(11)
        self.database.updateSyncStatus(11)
        self.database.saveActivities([11, 22, 22, 33])
        self.assertEqual(self.database.getKnownActivityIds(), {11, 22, 33})
        self.assertEqual(self.database.getUnSyncActivity(), [22, 33])
        with closing(sqlite3.connect(self.database_dir / "garmin.db")) as database:
            self.assertTrue(database.execute("PRAGMA index_list(garmin_activity)").fetchall())

    def test_index_is_added_to_old_database_without_changing_records(self):
        self.database.saveActivity(11)
        with closing(sqlite3.connect(self.database_dir / "garmin.db")) as database:
            with database:
                database.execute("DROP INDEX garmin_activity_activity_id")
        self.assertEqual(self.database.getKnownActivityIds(), {11})
        self.assertEqual(self.database.getUnSyncActivity(), [11])


if __name__ == "__main__":
    unittest.main()
