from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from wengao_ppt.scene_images import (
    PixabayPhotoProvider,
    SceneImageError,
    ScenePhotoProvider,
    is_allowed_license,
    is_valid_pixabay_source_page,
    scene_metadata_matches_query,
)


class PixabayPhotoProviderTests(unittest.TestCase):
    def test_pixabay_license_requires_canonical_first_party_license_url(self):
        self.assertTrue(is_allowed_license(
            "Pixabay License", license_url="https://pixabay.com/service/license-summary/"))
        for url in (
            "",
            "https://pixabay.com.evil.example/service/license-summary/",
            "http://pixabay.com/service/license-summary/",
            "https://pixabay.com/service/license-summary/?copy=1",
            "https://pixabay.com/service/terms/",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_allowed_license("Pixabay License", license_url=url))

    def test_pixabay_source_attribution_requires_first_party_photo_page(self):
        self.assertTrue(is_valid_pixabay_source_page(
            "https://pixabay.com/photos/example-photo-127/"))
        for url in (
            "https://pixabay.com.evil.example/photos/example-127/",
            "https://pixabay.com/users/example-1/",
            "http://pixabay.com/photos/example-127/",
            "https://pixabay.com:444/photos/example-127/",
            "https://user@pixabay.com/photos/example-127/",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_valid_pixabay_source_page(url))

    def test_search_uses_environment_key_and_returns_attribution_without_key(self):
        api_key = "test-only-not-a-real-api-key"
        response = {
            "hits": [{
                "id": 127,
                "pageURL": "https://pixabay.com/photos/shopper-produce-supermarket-127/",
                "user": "Example Photographer",
                "tags": "shopper, selecting produce, supermarket, shopper, supermarket",
                "imageWidth": 1600,
                "imageHeight": 1000,
                "largeImageURL": "https://pixabay.com/get/g127_1280.jpg",
            }],
        }
        with patch.dict(os.environ, {"PIXABAY_API_KEY": api_key}, clear=True):
            with patch("wengao_ppt.scene_images._request_json", return_value=response) as request:
                rows = PixabayPhotoProvider().search("shopper selecting produce supermarket")

        self.assertEqual(len(rows), 1)
        request.assert_called_once()
        call = request.call_args
        # The Pixabay API takes the key as a query parameter, not a header.
        self.assertIn(f"key={api_key}", call.args[0])
        row = rows[0]
        self.assertEqual(row["provider"], "pixabay")
        self.assertEqual(row["creator"], "Example Photographer")
        # Repeated tags are deduplicated before becoming the caption.
        self.assertEqual(row["description"], "shopper, selecting produce, supermarket")
        self.assertTrue(scene_metadata_matches_query(
            "shopper selecting produce supermarket", row,
            scene_context={"photo_search_terms": {"subject": "shopper",
                                                  "action": "selecting produce",
                                                  "setting": "supermarket"}}))
        self.assertEqual(row["license"], "Pixabay License")
        self.assertEqual(row["license_url"], "https://pixabay.com/service/license-summary/")
        self.assertNotIn(api_key, json.dumps(row))

    def test_search_fails_closed_without_key_and_does_not_echo_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SceneImageError) as caught:
                PixabayPhotoProvider().search("shopper selecting produce supermarket")
        self.assertEqual(caught.exception.code, "SCENE.PIXABAY_API_KEY_MISSING")
        self.assertNotIn("key=", caught.exception.detail)

    def test_default_provider_order_is_opt_in_and_ignores_blank_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"PIXABAY_API_KEY": "  "}, clear=True):
                no_key = ScenePhotoProvider(tmp)
            self.assertEqual([p.name for p in no_key.providers],
                             ["wikimedia-commons", "openverse"])

            with patch.dict(os.environ, {"PIXABAY_API_KEY": "configured-test-key"}, clear=True):
                configured = ScenePhotoProvider(tmp)
            self.assertEqual([p.name for p in configured.providers],
                             ["pixabay", "wikimedia-commons", "openverse"])

            with patch.dict(os.environ, {"PEXELS_API_KEY": "pexels-key",
                                         "PIXABAY_API_KEY": "pixabay-key"}, clear=True):
                both = ScenePhotoProvider(tmp)
            self.assertEqual([p.name for p in both.providers],
                             ["pexels", "pixabay", "wikimedia-commons", "openverse"])

    def test_auth_error_is_generic_and_does_not_expose_key(self):
        api_key = "test-only-not-a-real-api-key"

        class UnauthorizedOpener:
            def open(self, request, timeout):
                self.requested_url = request.full_url
                raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

        opener = UnauthorizedOpener()
        with patch.dict(os.environ, {"PIXABAY_API_KEY": api_key}, clear=True):
            with patch("wengao_ppt.scene_images.urllib.request.build_opener",
                       return_value=opener):
                with self.assertRaises(SceneImageError) as caught:
                    PixabayPhotoProvider().search("shopper selecting produce supermarket")
        self.assertIn(api_key, opener.requested_url)
        self.assertEqual(caught.exception.code, "SCENE.PROVIDER_AUTH_FAILED")
        self.assertNotIn(api_key, caught.exception.detail)


if __name__ == "__main__":
    unittest.main()
