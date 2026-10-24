from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from wengao_ppt.scene_images import (
    PexelsPhotoProvider,
    SceneImageError,
    ScenePhotoProvider,
    is_allowed_license,
    is_valid_pexels_source_page,
    scene_metadata_matches_query,
)


class PexelsPhotoProviderTests(unittest.TestCase):
    def test_pexels_license_requires_canonical_first_party_license_url(self):
        self.assertTrue(is_allowed_license(
            "Pexels License", license_url="https://www.pexels.com/license/"))
        for url in (
            "",
            "https://pexels.com.evil.example/license/",
            "http://www.pexels.com/license/",
            "https://www.pexels.com/license/?copy=1",
            "https://www.pexels.com/terms/",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_allowed_license("Pexels License", license_url=url))

    def test_pexels_source_attribution_requires_first_party_photo_page(self):
        self.assertTrue(is_valid_pexels_source_page(
            "https://www.pexels.com/photo/example-photo-127/"))
        for url in (
            "https://pexels.com.evil.example/photo/example-127/",
            "https://www.pexels.com/search/example/",
            "http://www.pexels.com/photo/example-127/",
            "https://www.pexels.com:444/photo/example-127/",
            "https://user@www.pexels.com/photo/example-127/",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_valid_pexels_source_page(url))

    def test_search_uses_environment_key_and_returns_attribution_without_key(self):
        api_key = "test-only-not-a-real-api-key"
        response = {
            "photos": [{
                "id": 127,
                "url": "https://www.pexels.com/photo/shopper-selecting-fresh-produce-in-a-supermarket-127/",
                "photographer": "Example Photographer",
                "alt": "",
                "width": 1600,
                "height": 1000,
                "src": {"large2x": "https://images.pexels.com/photos/127/photo.jpeg"},
            }],
        }
        with patch.dict(os.environ, {"PEXELS_API_KEY": api_key}, clear=True):
            with patch("wengao_ppt.scene_images._request_json", return_value=response) as request:
                rows = PexelsPhotoProvider().search("shopper selecting produce supermarket")

        self.assertEqual(len(rows), 1)
        request.assert_called_once()
        call = request.call_args
        self.assertEqual(call.kwargs["headers"]["Authorization"], api_key)
        row = rows[0]
        self.assertEqual(row["provider"], "pexels")
        self.assertEqual(row["creator"], "Example Photographer")
        self.assertEqual(row["title"], "shopper selecting fresh produce in a supermarket")
        self.assertIn("shopper selecting fresh produce", row["description"])
        self.assertTrue(scene_metadata_matches_query(
            "shopper selecting produce supermarket", row,
            scene_context={"photo_search_terms": {"subject": "shopper",
                                                  "action": "selecting produce",
                                                  "setting": "supermarket"}}))
        self.assertEqual(row["license"], "Pexels License")
        self.assertEqual(row["license_url"], "https://www.pexels.com/license/")
        self.assertNotIn(api_key, json.dumps(row))

    def test_search_fails_closed_without_key_and_does_not_echo_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SceneImageError) as caught:
                PexelsPhotoProvider().search("shopper selecting produce supermarket")
        self.assertEqual(caught.exception.code, "SCENE.PEXELS_API_KEY_MISSING")
        self.assertNotIn("Authorization", caught.exception.detail)

    def test_default_provider_order_is_opt_in_and_ignores_blank_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"PEXELS_API_KEY": "  "}, clear=True):
                no_key = ScenePhotoProvider(tmp)
            self.assertEqual([p.name for p in no_key.providers],
                             ["wikimedia-commons", "openverse"])

            with patch.dict(os.environ, {"PEXELS_API_KEY": "configured-test-key"}, clear=True):
                configured = ScenePhotoProvider(tmp)
            self.assertEqual([p.name for p in configured.providers],
                             ["pexels", "wikimedia-commons", "openverse"])

    def test_auth_error_is_generic_and_does_not_expose_key(self):
        api_key = "test-only-not-a-real-api-key"

        class UnauthorizedOpener:
            def open(self, request, timeout):
                self.authorization = request.get_header("Authorization")
                raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

        opener = UnauthorizedOpener()
        with patch.dict(os.environ, {"PEXELS_API_KEY": api_key}, clear=True):
            with patch("wengao_ppt.scene_images.urllib.request.build_opener",
                       return_value=opener):
                with self.assertRaises(SceneImageError) as caught:
                    PexelsPhotoProvider().search("shopper selecting produce supermarket")
        self.assertEqual(opener.authorization, api_key)
        self.assertEqual(caught.exception.code, "SCENE.PROVIDER_AUTH_FAILED")
        self.assertNotIn(api_key, caught.exception.detail)


if __name__ == "__main__":
    unittest.main()
