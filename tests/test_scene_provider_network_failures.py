from __future__ import annotations

import io
import random
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from PIL import Image

from wengao_ppt import scene_images
from wengao_ppt.scene_images import SceneImageError, ScenePhotoProvider


class SceneProviderNetworkFailureTests(unittest.TestCase):
    def test_search_tls_handshake_timeout_retries_then_is_classified_without_raw_detail(self):
        class Opener:
            def __init__(self):
                self.calls = 0

            def open(self, request, timeout):
                self.calls += 1
                raise urllib.error.URLError(
                    TimeoutError("TLS handshake timed out; private-query-marker"))

        opener = Opener()
        with patch("wengao_ppt.scene_images.urllib.request.build_opener",
                   return_value=opener):
            with self.assertRaises(SceneImageError) as caught:
                scene_images._request_json(
                    "https://api.openverse.org/v1/images/?q=neutral",
                    {"api.openverse.org"}, timeout_s=1, max_attempts=2)

        self.assertEqual(caught.exception.code,
                         "SCENE.PROVIDER_TLS_HANDSHAKE_TIMEOUT")
        self.assertEqual(opener.calls, 2)
        self.assertNotIn("private-query-marker", caught.exception.detail)

    def test_image_tls_handshake_timeout_has_distinct_structured_code(self):
        class Opener:
            def open(self, request, timeout):
                raise urllib.error.URLError(TimeoutError("TLS handshake timed out"))

        with patch("wengao_ppt.scene_images.urllib.request.build_opener",
                   return_value=Opener()):
            with self.assertRaises(SceneImageError) as caught:
                scene_images._download_image(
                    "https://media.example.org/photo.jpg", {"media.example.org"},
                    timeout_s=1)

        self.assertEqual(caught.exception.code,
                         "SCENE.IMAGE_TLS_HANDSHAKE_TIMEOUT")

    def test_search_handshake_timeout_stops_synonym_attempts(self):
        calls = []

        def fail_search(query, timeout_s):
            calls.append(query)
            raise SceneImageError("SCENE.PROVIDER_TLS_HANDSHAKE_TIMEOUT",
                                  "fixture handshake timeout")

        context = {"photo_search_terms": {
            "subject": "shopper",
            "action": "choosing groceries",
            "setting": "supermarket",
        }}
        with self.assertRaises(SceneImageError):
            scene_images._bounded_scene_search(
                fail_search, "shopper choosing groceries supermarket", context,
                limit=8)
        self.assertEqual(len(calls), 1)

    def test_search_circuit_breaker_is_job_local_and_other_provider_fallback_runs(self):
        class FailingSearch:
            name = "provider-one"
            allowed_hosts = {"api.one.example"}

            def __init__(self):
                self.calls = 0

            def search(self, query):
                self.calls += 1
                raise SceneImageError("SCENE.PROVIDER_TLS_HANDSHAKE_TIMEOUT",
                                      "fixture handshake timeout")

        class WorkingSearch:
            name = "provider-two"
            allowed_hosts = {"media.two.example"}

            def __init__(self):
                self.calls = 0

            def search(self, query):
                self.calls += 1
                return [{
                    "provider": self.name,
                    "source_key": "shopper-1",
                    "title": "Shopper choosing groceries at supermarket",
                    "description": "A shopper selects groceries in a supermarket.",
                    "license": "CC0",
                    "source_page": "https://commons.wikimedia.org/wiki/File:shopper.jpg",
                    "download_url": "https://media.two.example/shopper.jpg",
                    "width": 1200,
                    "height": 800,
                }]

        image_buffer = io.BytesIO()
        # A downloaded scene photo must be photographic: the provider rejects
        # flat single-colour art, so the fixture carries per-pixel grain.
        base = Image.new("RGB", (800, 600), (120, 160, 180))
        grain = Image.frombytes(
            "RGB", (800, 600),
            random.Random(11).randbytes(800 * 600 * 3))
        Image.blend(base, grain, 0.7).save(image_buffer, format="PNG")
        image_bytes = image_buffer.getvalue()
        first, second = FailingSearch(), WorkingSearch()

        with tempfile.TemporaryDirectory() as cache:
            provider = ScenePhotoProvider(cache, providers=[first, second])
            with patch("wengao_ppt.scene_images._download_image",
                       return_value=(image_bytes, "PNG", (800, 600))):
                result = provider.fetch_one(
                    "shopper choosing groceries supermarket")
                self.assertEqual(result["provider"], "provider-two")
                with self.assertRaises(SceneImageError):
                    provider.fetch_one("another shopper at a grocery store")

        self.assertEqual(first.calls, 1)
        # The second provider remains active for each distinct later scene.
        self.assertEqual(second.calls, 2)
        self.assertIn("provider-one", provider.disabled_providers)
        self.assertNotIn("provider-two", provider.disabled_providers)
        # A new job gets a new provider instance and can retry the recovered provider.
        with tempfile.TemporaryDirectory() as next_cache:
            next_job_provider = ScenePhotoProvider(next_cache, providers=[first])
            self.assertNotIn("provider-one", next_job_provider.disabled_providers)
            with self.assertRaises(SceneImageError):
                next_job_provider.fetch_one("shopper choosing groceries supermarket")
            self.assertEqual(first.calls, 2)
            self.assertIn("provider-one", next_job_provider.disabled_providers)

    def test_image_cdn_handshake_skips_only_that_host_for_same_job(self):
        record = {
            "provider": "fixture",
            "source_key": "photo-1",
            "title": "Ceramic mug on a table",
            "description": "A ceramic mug for drinking water.",
            "license": "CC0",
            "source_page": "https://commons.wikimedia.org/wiki/File:photo-1.jpg",
            "download_url": "https://MEDIA.example.org/photo.jpg",
            "width": 1200,
            "height": 800,
        }

        class Search:
            name = "fixture"
            allowed_hosts = {"media.example.org"}

            def __init__(self):
                self.search_calls = 0

            def search(self, query):
                self.search_calls += 1
                return [record]

        class DownloadFailure:
            def __init__(self):
                self.calls = 0

            def __call__(self, url, hosts):
                self.calls += 1
                raise SceneImageError("SCENE.IMAGE_TLS_HANDSHAKE_TIMEOUT",
                                      "fixture image handshake timeout")

        search = Search()
        download = DownloadFailure()
        with tempfile.TemporaryDirectory() as cache:
            provider = ScenePhotoProvider(cache, providers=[search])
            with patch("wengao_ppt.scene_images._download_image", side_effect=download):
                for _ in range(2):
                    with self.assertRaises(SceneImageError):
                        provider.fetch_one("ceramic mug")

        self.assertEqual(search.search_calls, 2)
        self.assertEqual(download.calls, 1)
        self.assertEqual(provider.disabled_download_hosts, {"media.example.org"})
        self.assertNotIn("fixture", provider.disabled_providers)


if __name__ == "__main__":
    unittest.main()
