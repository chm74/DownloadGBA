import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from gba_wfs_client import (  # noqa: E402
    GbaWfsClient,
    WfsBboxTooLargeError,
    WfsError,
    WfsRateLimitError,
    get_thread_client,
)


def payload(count: int, matched: int | None = None) -> dict:
    features = [
        {"type": "Feature", "properties": {"id": str(index)}, "geometry": None}
        for index in range(count)
    ]
    return {"features": features, "numberMatched": count if matched is None else matched}


class FetchSplitTests(unittest.TestCase):
    def test_splits_when_span_exceeds_max(self):
        client = GbaWfsClient(min_request_interval=0, max_span=0.1, min_span=0.01, sleep_seconds=0)
        calls: list[tuple[float, float, float, float]] = []

        def fake_request(minx, miny, maxx, maxy):
            calls.append((minx, miny, maxx, maxy))
            return payload(1)

        with patch.object(client, "_request_features", side_effect=fake_request):
            features, stats = client.fetch_bbox_features(0.0, 0.0, 0.25, 0.25)

        self.assertEqual(len(calls), 16)
        self.assertEqual(len(features), 16)
        self.assertEqual(stats.requests, 16)
        self.assertEqual(stats.splits, 5)

    def test_truncated_response_triggers_split(self):
        client = GbaWfsClient(min_request_interval=0, max_span=0.1, min_span=0.005, sleep_seconds=0)
        parent = (0.0, 0.0, 0.08, 0.08)
        calls: list[tuple[float, float, float, float]] = []

        def fake_request(minx, miny, maxx, maxy):
            bounds = (minx, miny, maxx, maxy)
            calls.append(bounds)
            if bounds == parent:
                return payload(2, matched=10)
            return payload(1)

        with patch.object(client, "_request_features", side_effect=fake_request):
            features, stats = client.fetch_bbox_features(*parent)

        self.assertEqual(len(calls), 5)
        self.assertEqual(len(features), 4)
        self.assertEqual(stats.splits, 1)

    def test_truncation_at_min_span_raises(self):
        client = GbaWfsClient(min_request_interval=0, max_span=0.1, min_span=0.1, sleep_seconds=0)
        with patch.object(client, "_request_features", return_value=payload(1, matched=5)):
            with self.assertRaises(WfsError):
                client.fetch_bbox_features(0.0, 0.0, 0.1, 0.1)

    def test_bbox_too_large_error_splits(self):
        client = GbaWfsClient(min_request_interval=0, max_span=1.0, min_span=0.01, sleep_seconds=0)
        parent = (0.0, 0.0, 0.2, 0.2)
        calls: list[tuple[float, float, float, float]] = []

        def fake_request(minx, miny, maxx, maxy):
            bounds = (minx, miny, maxx, maxy)
            calls.append(bounds)
            if bounds == parent:
                raise WfsBboxTooLargeError("too large")
            return payload(1)

        with patch.object(client, "_request_features", side_effect=fake_request):
            features, stats = client.fetch_bbox_features(*parent)

        self.assertEqual(len(calls), 5)
        self.assertEqual(len(features), 4)
        self.assertEqual(stats.splits, 1)

    def test_failure_splits_until_min_span(self):
        client = GbaWfsClient(min_request_interval=0, max_span=0.1, min_span=0.02, sleep_seconds=0)
        parent = (0.0, 0.0, 0.1, 0.1)

        def fake_request(minx, miny, maxx, maxy):
            bounds = (minx, miny, maxx, maxy)
            if bounds[2] - bounds[0] > 0.03:
                raise WfsError("simulated failure")
            return payload(1)

        with patch.object(client, "_request_features", side_effect=fake_request):
            features, stats = client.fetch_bbox_features(*parent)

        self.assertEqual(len(features), 16)
        self.assertGreaterEqual(stats.splits, 1)


class RateLimitTests(unittest.TestCase):
    def test_429_is_retried_without_splitting(self):
        client = GbaWfsClient(min_request_interval=0, max_span=0.1, min_span=0.01, sleep_seconds=0)
        limited = Mock()
        limited.status_code = 429
        limited.headers = {"Retry-After": "0"}
        limited.text = "<html>429 Too Many Requests</html>"
        success = Mock()
        success.status_code = 200
        success.headers = {}
        success.text = ""
        success.json.return_value = {
            "features": [{"properties": {"id": "1"}, "geometry": None}],
            "numberMatched": 1,
        }

        with patch.object(client, "_ensure_session"):
            client._token = "test-token"
            with patch.object(client.session, "get", side_effect=[limited, success]) as get_mock:
                with patch("gba_wfs_client.time.sleep"):
                    payload = client._request_features(0, 0, 0.05, 0.05)

        self.assertEqual(len(payload["features"]), 1)
        self.assertEqual(get_mock.call_count, 2)

    def test_rate_limit_error_never_splits_tiles(self):
        client = GbaWfsClient(min_request_interval=0, max_span=0.1, min_span=0.01, sleep_seconds=0)
        with patch.object(
            client,
            "_request_features",
            side_effect=WfsRateLimitError("persistent 429"),
        ):
            with self.assertRaises(WfsRateLimitError):
                client.fetch_bbox_features(0, 0, 0.05, 0.05)


class ThreadLocalClientTests(unittest.TestCase):
    def test_get_thread_client_is_thread_local(self):
        main_client = get_thread_client()
        self.assertIs(main_client, get_thread_client())

        results: dict = {}

        def worker():
            results["client"] = get_thread_client()

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

        self.assertIsNotNone(results.get("client"))
        self.assertIsNot(results["client"], main_client)


if __name__ == "__main__":
    unittest.main()
