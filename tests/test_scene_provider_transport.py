"""Provider transport, URL-safety and photographic-integrity regression tests.

These tests cover the defects found while diagnosing the real 13-page job
(job-167a1306f549, 17/17 scenes unresolved):

* every provider hostname resolved to a private fake-IP (proxy DNS), so the
  old "must resolve to a global IP" gate discarded every candidate and the
  failure surfaced as "no licensed photo";
* a silent license/URL drop produced the same misleading message even when the
  archive had returned rows;
* the per-query timeout (4 s) was far below measured archive latency
  (5-35 s uncached), so no uncached query could ever succeed;
* a Wikimedia line-art illustration was accepted as a scene photograph because
  the style gate only looked for an explicit negative word in the metadata.

All tests are offline: network entry points are stubbed.
"""
import hashlib
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

from wengao_ppt import scene_images as si
from wengao_ppt.scene_images import (OpenversePhotoProvider, PexelsPhotoProvider,
                                     SceneImageError, ScenePhotoProvider,
                                     WikimediaCommonsPhotoProvider,
                                     keyword_search_variants,
                                     photographic_asset_check)


def flat_illustration_bytes() -> bytes:
    """Line-art-like asset: a handful of flat fills, no texture."""
    image = Image.new("RGB", (900, 600), (250, 250, 245))
    draw = ImageDraw.Draw(image)
    draw.rectangle((60, 80, 420, 520), fill=(235, 228, 205), outline=(20, 20, 20), width=4)
    draw.rectangle((480, 90, 840, 520), fill=(214, 232, 240), outline=(20, 20, 20), width=4)
    draw.ellipse((150, 120, 320, 300), fill=(226, 205, 170), outline=(20, 20, 20), width=4)
    draw.ellipse((560, 130, 720, 300), fill=(232, 214, 180), outline=(20, 20, 20), width=4)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def photograph_bytes(seed: int = 7) -> bytes:
    """Photo-like asset: per-pixel colour variation instead of flat fills."""
    image = Image.new("RGB", (900, 600))
    pixels = image.load()
    for y in range(600):
        for x in range(900):
            pixels[x, y] = ((x * 7 + seed * 13) % 256,
                            (y * 5 + seed * 29) % 256,
                            ((x + y) * 3 + seed * 41) % 256)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


def openverse_payload(count: int = 3) -> dict:
    return {"result_count": count, "results": [
        {"id": f"id-{i}", "title": f"Shoppers browsing produce supermarket {i}",
         "description": "Shoppers browsing produce in a supermarket aisle",
         "creator": "Fixture", "license": "cc0", "license_version": "1.0",
         "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
         "url": f"https://images.example.org/id-{i}.jpg",
         "thumbnail": f"https://thumbs.example.org/id-{i}.jpg",
         "foreign_landing_url": f"https://commons.wikimedia.org/wiki/File:{i}.jpg",
         "detail_url": f"https://api.openverse.org/v1/images/id-{i}/",
         "attribution": "fixture", "width": 2400, "height": 1600,
         "filetype": "jpg"} for i in range(count)]}


class StubProvider:
    """Minimal provider whose records and download bytes are controlled."""

    def __init__(self, records, data, *, name="wikimedia-commons"):
        self.name = name
        self.allowed_hosts = {"images.example.org"}
        self.records = records
        self.data = data
        self.queries = []

    def search(self, query, limit=16):
        self.queries.append(query)
        return list(self.records)


def record(**overrides) -> dict:
    base = {
        "provider": "wikimedia-commons", "source_key": "File:Fixture.jpg",
        "title": "Doctor consulting patient clinic",
        "description": "A doctor consulting a patient in a clinic room",
        "creator": "Fixture", "license": "CC0", "license_version": "1.0",
        "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
        "source_page": "https://commons.wikimedia.org/wiki/File:Fixture.jpg",
        "download_url": "https://images.example.org/fixture.jpg",
        "attribution": "Fixture", "width": 2400, "height": 1600,
    }
    base.update(overrides)
    return base


SCENE = {
    "visual_query": "doctor consulting patient clinic",
    "metadata_query": "doctor consulting patient clinic",
    "photo_search_terms": {"subject": "doctor", "action": "consulting patient",
                           "setting": "clinic"},
    "style": "documentary photography",
}

SHOPPING_SCENE = {
    "visual_query": "shoppers browsing produce supermarket",
    "metadata_query": "shoppers browsing produce supermarket",
    "photo_search_terms": {"subject": "shoppers", "action": "browsing produce",
                           "setting": "supermarket"},
    "style": "documentary photography",
}


class PublicHttpsGateTests(unittest.TestCase):
    def test_public_hostnames_pass_and_internal_targets_fail(self):
        accepted = [
            "https://example.com/x.jpg",
            "https://api.openverse.org/v1/images/abc/thumb/",
            "https://8.8.8.8/x.jpg",
        ]
        rejected = [
            "http://example.com/x.jpg",
            "https://127.0.0.1/x.jpg",
            "https://192.168.1.5/x.jpg",
            "https://10.0.0.9/x.jpg",
            "https://172.19.0.234/x.jpg",
            "https://localhost/x.jpg",
            "https://printer.local/x.jpg",
            "https://svc.internal/x.jpg",
            "https://user:pw@example.com/x.jpg",
            "https://intranet/x.jpg",
            "https://[::1]/x.jpg",
            "https://[fd00::1]/x.jpg",
        ]
        for url in accepted:
            with self.subTest(url=url):
                self.assertTrue(si._url_is_public_https(url))
        for url in rejected:
            with self.subTest(url=url):
                self.assertFalse(si._url_is_public_https(url))

    def test_fake_ip_dns_does_not_discard_provider_rows(self):
        # A proxy that answers every name with one private address used to
        # empty every search result. Hostname shape decides, not resolution.
        def fake_getaddrinfo(host, port, *args, **kwargs):
            return [(2, 1, 6, "", ("172.19.0.234", port))]

        with mock.patch.object(si.socket, "getaddrinfo", fake_getaddrinfo), \
                mock.patch.object(si, "_request_json",
                                  lambda *a, **k: openverse_payload(3)):
            rows = si.OpenversePhotoProvider().search("supermarket shopping", limit=3)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["download_url"].startswith("https://") for row in rows))

    def test_resolution_report_is_diagnostic_only(self):
        report = si._host_resolution_report("api.openverse.org")
        self.assertEqual(report["host"], "api.openverse.org")
        self.assertIn("resolved", report)


class OpenverseRowAuditTests(unittest.TestCase):
    def _search(self, item_overrides=None):
        payload = openverse_payload(1)
        payload["results"][0].update(item_overrides or {})
        provider = OpenversePhotoProvider()
        with mock.patch.object(si, "_request_json", return_value=payload):
            rows = provider.search("shoppers browsing produce supermarket", limit=1)
        return provider, rows

    def _fetch_without_network(self, provider, rows):
        provider.search_scene = lambda query, **kwargs: rows
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(si, "_download_image",
                                  side_effect=AssertionError("unsafe row reached download")):
            return ScenePhotoProvider(Path(tmp), providers=[provider]).fetch_one(
                "shoppers browsing produce supermarket", scene_context=SHOPPING_SCENE)

    def test_unlicensed_openverse_row_is_retained_for_audited_rejection(self):
        provider, rows = self._search({
            "license": "by-sa", "license_version": "4.0",
            "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
        })
        self.assertEqual(len(rows), 1)
        with self.assertRaises(SceneImageError) as ctx:
            self._fetch_without_network(provider, rows)
        self.assertEqual(ctx.exception.code, "SCENE.PROVIDER_ROWS_INELIGIBLE")
        self.assertIn("SCENE.LICENSE_NOT_ALLOWED", ctx.exception.detail)

    def test_openverse_original_media_url_is_preferred_and_host_pinned(self):
        provider, rows = self._search({
            "url": "https://media.provider.example/asset.jpg",
            "thumbnail": "https://thumbnail.provider.example/asset.jpg",
        })
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["download_url"], "https://media.provider.example/asset.jpg")
        observed = []

        def fake_download(url, hosts):
            observed.append((url, set(hosts)))
            return photograph_bytes(), "JPEG", (900, 600)

        provider.search_scene = lambda query, **kwargs: rows
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(si, "_download_image", side_effect=fake_download):
            result = ScenePhotoProvider(Path(tmp), providers=[provider]).fetch_one(
                "shoppers browsing produce supermarket", scene_context=SHOPPING_SCENE)
        self.assertEqual(result["download_url"], "https://media.provider.example/asset.jpg")
        self.assertEqual(observed, [(
            "https://media.provider.example/asset.jpg", {"media.provider.example"})])

    def test_openverse_media_url_rejects_nonstandard_https_port(self):
        provider, rows = self._search({
            "url": "https://media.provider.example:8443/asset.jpg",
        })
        provider.search_scene = lambda query, **kwargs: rows
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(si, "_download_image",
                                  side_effect=AssertionError("unsafe port reached download")):
            with self.assertRaises(SceneImageError) as ctx:
                ScenePhotoProvider(Path(tmp), providers=[provider]).fetch_one(
                    "shoppers browsing produce supermarket", scene_context=SHOPPING_SCENE)
        self.assertEqual(ctx.exception.code, "SCENE.PROVIDER_ROWS_INELIGIBLE")
        self.assertIn("SCENE.PHOTO_URL_NOT_PUBLIC_HTTPS", ctx.exception.detail)

    def test_openverse_media_redirect_stays_on_candidate_host(self):
        handler = si._HostRedirectHandler({"media.provider.example"})
        request = si.urllib.request.Request("https://media.provider.example/asset.jpg")
        with self.assertRaises(SceneImageError) as ctx:
            handler.redirect_request(request, None, 302, "Found", {},
                                     "https://other.provider.example/asset.jpg")
        self.assertEqual(ctx.exception.code, "SCENE.IMAGE_REDIRECT_BLOCKED")

    def test_missing_safe_openverse_source_page_is_recorded(self):
        provider, rows = self._search({
            "foreign_landing_url": "http://unsafe.example/image",
            "detail_url": "http://unsafe.example/detail",
        })
        self.assertEqual(len(rows), 1)
        with self.assertRaises(SceneImageError) as ctx:
            self._fetch_without_network(provider, rows)
        self.assertEqual(ctx.exception.code, "SCENE.PROVIDER_ROWS_INELIGIBLE")
        self.assertIn("SCENE.SOURCE_PAGE_NOT_PUBLIC_HTTPS", ctx.exception.detail)


class TransientProviderTlsRetryTests(unittest.TestCase):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, limit=-1):
            return b"{}"

    class FlakyOpener:
        def __init__(self, failures_before_success=None, always_fail=False):
            self.calls = 0
            self.failures_before_success = failures_before_success or 0
            self.always_fail = always_fail

        def open(self, request, timeout=None):
            self.calls += 1
            if self.always_fail or self.calls <= self.failures_before_success:
                raise urllib.error.URLError(TimeoutError("TLS handshake timed out"))
            return TransientProviderTlsRetryTests.Response()

    def test_tls_handshake_timeout_retries_once_and_recovers(self):
        opener = self.FlakyOpener(failures_before_success=1)
        with mock.patch.object(si.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(si.time, "sleep") as sleep:
            result = si._request_json(
                "https://api.openverse.org/v1/images/?q=shop", {"api.openverse.org"},
                timeout_s=1, max_attempts=2)
        self.assertEqual(result, {})
        self.assertEqual(opener.calls, 2)
        sleep.assert_called_once_with(0.4)

    def test_repeated_tls_handshake_timeout_is_reported_after_two_attempts(self):
        opener = self.FlakyOpener(always_fail=True)
        with mock.patch.object(si.urllib.request, "build_opener", return_value=opener), \
                mock.patch.object(si.time, "sleep"):
            with self.assertRaises(SceneImageError) as ctx:
                si._request_json(
                    "https://api.openverse.org/v1/images/?q=shop", {"api.openverse.org"},
                    timeout_s=1, max_attempts=99)
        self.assertEqual(ctx.exception.code, "SCENE.PROVIDER_TLS_HANDSHAKE_TIMEOUT")
        self.assertEqual(opener.calls, 2)

    def test_scene_search_adapters_request_only_two_transport_attempts(self):
        cases = [
            (WikimediaCommonsPhotoProvider(), "_search_query"),
            (OpenversePhotoProvider(), "search"),
            (PexelsPhotoProvider(), "search"),
        ]
        for provider, method in cases:
            with self.subTest(provider=provider.name):
                attempts = []
                def empty_results(*args, **kwargs):
                    attempts.append(kwargs.get("max_attempts"))
                    return []
                with mock.patch.object(provider, method, side_effect=empty_results):
                    provider.search_scene(
                        "doctor consulting patient clinic", scene_context=SCENE, limit=1)
                self.assertTrue(attempts)
                self.assertEqual(set(attempts), {2})


class KeywordVariantTests(unittest.TestCase):
    def test_variants_are_short_and_use_only_anchor_words(self):
        variants = keyword_search_variants(
            SCENE, "doctor consulting patient clinic")
        self.assertTrue(variants)
        anchor_words = {"doctor", "consulting", "patient", "clinic"}
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertLessEqual(len(variant.split()), 3)
                self.assertTrue(set(variant.split()) <= anchor_words, variant)

    def test_variants_keep_the_canonical_query_as_last_resort(self):
        variants = keyword_search_variants(
            SCENE, "doctor consulting patient clinic", max_variants=8)
        self.assertIn("doctor consulting patient clinic", variants)

    def test_no_anchors_falls_back_to_the_canonical_query(self):
        self.assertEqual(keyword_search_variants(None, "doctor clinic"),
                         ["doctor clinic"])


class ProviderFailureClassificationTests(unittest.TestCase):
    def _fetch(self, provider, data=None):
        payload = provider.data if data is None else data
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(si, "_download_image",
                                  lambda url, hosts: (payload, "JPEG", (900, 600))):
            return ScenePhotoProvider(Path(tmp), providers=[provider]).fetch_one(
                "doctor consulting patient clinic", scene_context=SCENE)

    def test_transport_failure_is_not_reported_as_no_licensed_photo(self):
        class DownProvider(StubProvider):
            def search(self, query, limit=16):
                raise SceneImageError("SCENE.PROVIDER_TLS_HANDSHAKE_TIMEOUT",
                                      "图片检索服务的 HTTPS 握手等待超时。")
        with self.assertRaises(SceneImageError) as ctx:
            self._fetch(DownProvider([], b""))
        self.assertEqual(ctx.exception.code, "SCENE.PROVIDER_TLS_HANDSHAKE_TIMEOUT")
        self.assertNotIn("没有找到许可合规", ctx.exception.detail)

    def test_license_rejection_is_recorded_not_silent(self):
        provider = StubProvider([record(license="CC BY-SA 4.0",
                                        license_url="https://creativecommons.org/licenses/by-sa/4.0/")],
                                photograph_bytes())
        with self.assertRaises(SceneImageError) as ctx:
            self._fetch(provider)
        self.assertEqual(ctx.exception.code, "SCENE.PROVIDER_ROWS_INELIGIBLE")
        self.assertIn("SCENE.LICENSE_NOT_ALLOWED", ctx.exception.detail)

    def test_flat_illustration_is_rejected_before_insertion(self):
        provider = StubProvider([record()], flat_illustration_bytes())
        with self.assertRaises(SceneImageError) as ctx:
            self._fetch(provider)
        self.assertEqual(ctx.exception.code, "SCENE.PROVIDER_ROWS_INELIGIBLE")
        self.assertIn("SCENE.PHOTO_NOT_PHOTOGRAPHIC", ctx.exception.detail)

    def test_real_photo_bytes_are_accepted(self):
        provider = StubProvider([record()], photograph_bytes())
        result = self._fetch(provider)
        self.assertEqual(result["sha256"],
                         hashlib.sha256(photograph_bytes()).hexdigest())
        self.assertEqual(result["provider"], "wikimedia-commons")


class PhotographicGuardTests(unittest.TestCase):
    def test_flat_art_and_photograph_are_separated(self):
        flat = photographic_asset_check(flat_illustration_bytes())
        photo = photographic_asset_check(photograph_bytes())
        self.assertFalse(flat["photographic"], flat)
        self.assertTrue(photo["photographic"], photo)
        self.assertGreater(photo["unique_5bit"], flat["unique_5bit"])

    def test_undecodable_bytes_are_not_accepted(self):
        result = photographic_asset_check(b"not an image")
        self.assertFalse(result["photographic"])


class ProviderBudgetTests(unittest.TestCase):
    def test_query_timeout_matches_measured_archive_latency(self):
        # Measured 2026-10-23 on this host: Wikimedia 5.0-5.3 s, Openverse
        # cache hits ~0.3 s and uncached queries 5-35 s. A lower bound below
        # the actual provider latency guarantees that candidates are discarded.
        self.assertGreaterEqual(
            si.SCENE_SEARCH_QUERY_TIMEOUT_BY_PROVIDER["openverse"], 15.0)
        self.assertGreaterEqual(
            si.SCENE_SEARCH_QUERY_TIMEOUT_BY_PROVIDER["wikimedia-commons"], 8.0)
        for provider, seconds in si.SCENE_SEARCH_BUDGET_BY_PROVIDER.items():
            with self.subTest(provider=provider):
                self.assertGreaterEqual(
                    seconds, si.SCENE_SEARCH_QUERY_TIMEOUT_BY_PROVIDER[provider])


if __name__ == "__main__":
    unittest.main()
