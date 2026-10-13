import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.garmin import garmin_sync_coros
from garmin.garmin_client import GarminClient
from scripts.utils.activity_scan import ScanState


class ActivityScanTest(unittest.TestCase):
    def garmin(self, pages, newest=0):
        client = GarminClient.__new__(GarminClient)
        client.newestNum = newest
        client.getActivities = Mock(side_effect=pages)
        return client

    def test_incremental_scan_stops_after_two_known_pages(self):
        known = set(range(200))
        pages = [[{"activityId": i} for i in range(100)], [{"activityId": i} for i in range(100, 200)]]
        client = self.garmin(pages)
        self.assertEqual(len(client.getAllActivities(known_ids=known)), 200)
        self.assertEqual(client.getActivities.call_count, 2)

    def test_new_activity_resets_known_page_boundary(self):
        known = set(range(400))
        pages = [[{"activityId": i} for i in range(100)],
                 [{"activityId": 999}] + [{"activityId": i} for i in range(101, 200)],
                 [{"activityId": i} for i in range(200, 300)],
                 [{"activityId": i} for i in range(300, 400)]]
        client = self.garmin(pages)
        result = client.getAllActivities(known_ids=known)
        self.assertIn({"activityId": 999}, result)
        self.assertEqual(client.getActivities.call_count, 4)

    def test_full_scan_finds_older_imports_beyond_incremental_window(self):
        pages = [[{"activityId": i} for i in range(100)],
                 [{"activityId": i} for i in range(100, 200)], [{"activityId": 999}]]
        client = self.garmin(pages, newest=6)
        self.assertEqual(client.getAllActivities(known_ids=set(range(200)), full_scan=True)[-1], {"activityId": 999})
        self.assertEqual(client.getActivities.call_count, 3)

    def test_newest_limit_is_honored_without_overfetching(self):
        client = self.garmin([[{"activityId": i} for i in range(6)]], newest=6)
        self.assertEqual(len(client.getAllActivities()), 6)
        client.getActivities.assert_called_once_with(start=0, limit=6)

    def test_coros_scan_stops_at_known_boundary_and_does_not_fetch_extra_last_page(self):
        client = garmin_sync_coros.CorosClient.__new__(garmin_sync_coros.CorosClient)
        client.getActivities = Mock(side_effect=[
            {"data": {"totalPage": 5, "dataList": [{"labelId": 11}]}},
            {"data": {"totalPage": 5, "dataList": [{"labelId": 22}]}}])
        self.assertEqual(len(client.getAllActivities(known_ids={11, 22})), 2)
        self.assertEqual(client.getActivities.call_count, 2)
        client.getActivities = Mock(return_value={"data": {"totalPage": 1, "dataList": [{"labelId": 33}]}})
        self.assertEqual(client.getAllActivities(full_scan=True), [{"labelId": 33}])
        client.getActivities.assert_called_once()

    def test_full_scan_runs_initially_then_every_seven_days_and_on_request(self):
        with tempfile.TemporaryDirectory() as directory:
            state = ScanState(Path(directory) / "scan.json", clock=Mock(return_value=1000))
            self.assertTrue(state.should_full_scan({11}))
            state.mark_complete()
            self.assertFalse(state.should_full_scan({11}))
            self.assertTrue(state.should_full_scan(set()))
            self.assertTrue(state.should_full_scan({11}, requested=True))
            state.clock.return_value = 1000 + 7 * 86400
            self.assertTrue(state.should_full_scan({11}))
            self.assertEqual(state.path.stat().st_mode & 0o777, 0o600)

    def test_corrupt_scan_marker_requests_safe_full_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scan.json"
            path.write_text("invalid")
            self.assertTrue(ScanState(path).should_full_scan({11}))
