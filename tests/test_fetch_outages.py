import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import fetch_outages as scraper


def response(*ids):
    return {
        "geometryType": "esriGeometryPoint",
        "fields": [{"name": "F_OUTAGE_ID"}],
        "features": [
            {"attributes": {"F_OUTAGE_ID": id, "OBJECTID": 123,
                            "blueSkyNotificationSubscription": "ignored", "CITY": "SF"},
             "geometry": {"x": 1, "y": 2}}
            for id in ids
        ],
    }


class FetchTests(unittest.TestCase):
    def test_normal_snapshot_and_normalization(self):
        fetch = Mock(side_effect=[response("a"), {"count": 1}])
        self.assertEqual(scraper.fetch_snapshot(fetch), [
            {"F_OUTAGE_ID": "a", "CITY": "SF", "geometry_x": 1, "geometry_y": 2}
        ])

    def test_confirmed_empty(self):
        fetch = Mock(side_effect=[response(), {"count": 0}, response()])
        pause = Mock()
        self.assertEqual(scraper.fetch_snapshot(fetch, pause), [])
        pause.assert_called_once_with(5)
        self.assertEqual(fetch.call_args_list[1].kwargs, {"returnCountOnly": "true"})
        self.assertEqual(fetch.call_count, 3)

    def test_empty_must_be_confirmed(self):
        for replies in [
            [response(), {"count": 1}],
            [response(), {"count": 0}, response("a")],
            [response(), {"count": 0}, {"error": {"code": 500}}],
            [response(), {"count": 0}, URLError("offline")],
        ]:
            with self.subTest(replies=replies), self.assertRaises((ValueError, URLError)):
                scraper.fetch_snapshot(Mock(side_effect=replies), Mock())

    def test_reject_invalid_count(self):
        for count in ({}, {"count": None}, {"count": "1"}, {"count": True},
                      {"count": -1}, {"count": 2}, {"error": {"code": 500}}):
            with self.subTest(count=count), self.assertRaises(ValueError):
                scraper.fetch_snapshot(Mock(side_effect=[response("a"), count]))

    def test_reject_invalid_or_truncated_features(self):
        invalid = [None, [], {}, {"error": {"code": 500}},
                   {**response(), "features": None},
                   {**response(), "fields": []},
                   {**response(), "exceededTransferLimit": True},
                   {**response(), "geometryType": "esriGeometryPolygon"},
                   {**response(), "features": [{}]}, response("a", "a"),
                   response(None), response(123), response("")]
        no_geometry = response("a")
        del no_geometry["features"][0]["geometry"]
        invalid.append(no_geometry)
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                scraper.snapshot(data)

    def test_transport_and_json_failures_propagate(self):
        for error in (HTTPError("https://example.com", 503, "unavailable", {}, None),
                      URLError("offline"), TimeoutError("timeout")):
            with self.subTest(error=error), patch.object(scraper, "urlopen", side_effect=error):
                with self.assertRaises(type(error)):
                    scraper.query(outFields="*")
        with patch.object(scraper, "urlopen", return_value=io.BytesIO(b"not JSON")):
            with self.assertRaises(json.JSONDecodeError):
                scraper.query(outFields="*")

    def test_queries_use_timeout_and_distinct_cache_busters(self):
        with patch.object(scraper, "urlopen", side_effect=[io.BytesIO(b"{}"), io.BytesIO(b"{}")]) as get:
            scraper.query(outFields="*")
            scraper.query(outFields="*")
        self.assertNotEqual(get.call_args_list[0].args, get.call_args_list[1].args)
        self.assertEqual(get.call_args.kwargs, {"timeout": 30})

    def test_failed_fetch_preserves_archive_and_outputs(self):
        for failure in (URLError("offline"), {"error": {"code": 500}},
                        {**response(), "exceededTransferLimit": True}):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                old, new, message = [Path(directory) / name for name in ("old", "new", "message")]
                old.write_text('[{"F_OUTAGE_ID": "a"}]\n')
                before = old.read_bytes()
                with self.assertRaises((ValueError, URLError)):
                    scraper.prepare_update(old, new, message, Mock(side_effect=[failure]), Mock())
                self.assertEqual(old.read_bytes(), before)
                self.assertFalse(new.exists())
                self.assertFalse(message.exists())

    def test_prepare_empty_transition_then_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            old, new, message = [Path(directory) / name for name in ("old", "new", "message")]
            old.write_text('[{"F_OUTAGE_ID": "a"}]\n')
            scraper.prepare_update(old, new, message,
                                   Mock(side_effect=[response(), {"count": 0}, response()]), Mock())
            self.assertEqual(new.read_text(), "[]\n")
            self.assertIn("1 row removed", message.read_text())
            self.assertIn('"a"', old.read_text())
            old.write_text("[]\n")
            scraper.prepare_update(old, new, message,
                                   Mock(side_effect=[response(), {"count": 0}, response()]), Mock())
            self.assertEqual(new.read_bytes(), old.read_bytes())
            self.assertEqual(message.read_text(), "\n")


class DiffTests(unittest.TestCase):
    def test_empty_transitions_have_no_schema_changes(self):
        row = {"F_OUTAGE_ID": "a", "CITY": "SF"}
        for old, new, added, removed in [([], [], [], []),
                                       ([], [row], [row], []), ([row], [], [], [row])]:
            with self.subTest(old=old, new=new):
                self.assertEqual(scraper.diff_rows(old, new), {
                    "added": added, "removed": removed, "changed": [],
                    "columns_added": [], "columns_removed": [],
                })

    def test_regular_changes_keep_csv_diff_format(self):
        old = [{"F_OUTAGE_ID": "a", "CITY": "SF"}]
        new = copy.deepcopy(old)
        new[0]["CITY"] = "Oakland"
        self.assertEqual(scraper.diff_rows(old, new)["changed"], [
            {"key": "a", "changes": {"CITY": ["SF", "Oakland"]}}
        ])
        self.assertFalse(any(scraper.diff_rows(old, old).values()))


class CommitTests(unittest.TestCase):
    script = Path(__file__).resolve().parents[1] / "scripts/commit-outages.sh"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        (self.repo / "outages.json").write_text('[{"F_OUTAGE_ID":"a"}]\n')
        self.git("add", "outages.json")
        self.git("commit", "-m", "Initial")
        self.initial = self.git("rev-parse", "HEAD").stdout
        (self.repo / "message.txt").write_text("1 row removed\n")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True,
                              capture_output=True, text=True)

    def run_commit(self, text="[]\n"):
        (self.repo / "outages-new.json").write_text(text)
        return subprocess.run(["bash", str(self.script)], cwd=self.repo,
                              capture_output=True, text=True)

    def test_no_change_succeeds_without_remote(self):
        result = self.run_commit((self.repo / "outages.json").read_text())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, self.initial)

    def test_commit_failure_is_not_success(self):
        hook = self.repo / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.assertNotEqual(self.run_commit().returncode, 0)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, self.initial)

    def test_pull_failure_is_not_success(self):
        self.assertNotEqual(self.run_commit().returncode, 0)

    def test_commit_and_push_empty_snapshot_then_noop(self):
        remote = self.root / "remote.git"
        self.git("init", "--bare", str(remote))
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "-u", "origin", "main")
        result = self.run_commit()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("show", "origin/main:outages.json").stdout, "[]\n")
        head = self.git("rev-parse", "HEAD").stdout
        self.assertNotEqual(head, self.initial)
        self.assertEqual(self.run_commit().returncode, 0)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, head)

    def test_push_failure_is_not_success(self):
        remote = self.root / "remote.git"
        self.git("init", "--bare", str(remote))
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "-u", "origin", "main")
        hook = remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        self.assertNotEqual(self.run_commit().returncode, 0)


if __name__ == "__main__":
    unittest.main()
