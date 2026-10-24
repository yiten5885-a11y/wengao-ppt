from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import ANY, Mock, call, patch


class WorkbenchModelEndpointDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Importing the Workbench module constructs its in-process state. Keep
        # that import side effect inside an isolated temporary data root.
        cls.temp_root = tempfile.TemporaryDirectory(prefix="wengao-child-env-test-")
        saved = dict(os.environ)
        try:
            os.environ["WENGAO_DATA_ROOT"] = cls.temp_root.name
            os.environ.pop("WENGAO_LOCAL_MODEL_ENDPOINT", None)
            workbench_dir = str(Path(__file__).resolve().parents[1] / "workbench")
            if workbench_dir not in sys.path:
                sys.path.insert(0, workbench_dir)
            cls.server = importlib.import_module("workbench.server")
        finally:
            os.environ.clear()
            os.environ.update(saved)

    @classmethod
    def tearDownClass(cls):
        cls.temp_root.cleanup()

    def test_child_process_leaves_endpoint_unset_for_local_discovery(self):
        root = Path(self.temp_root.name) / "run-data"
        with patch.dict(os.environ, {}, clear=True):
            env = self.server.child_env(root)
        self.assertNotIn("WENGAO_LOCAL_MODEL_ENDPOINT", env)
        self.assertEqual(env["WENGAO_DATA_ROOT"], str(root))
        self.assertEqual(env["PYTHONPATH"], str(self.server.PROG))

    def test_explicit_loopback_endpoint_is_preserved(self):
        root = Path(self.temp_root.name) / "run-data"
        configured = "http://127.0.0.1:11434"
        with patch.dict(os.environ, {"WENGAO_LOCAL_MODEL_ENDPOINT": configured}, clear=True):
            env = self.server.child_env(root)
        self.assertEqual(env["WENGAO_LOCAL_MODEL_ENDPOINT"], configured)

    def test_scene_model_discovers_live_ollama_after_optional_bridge_refuses(self):
        from wengao_ppt import scene_model

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        seen = []

        def open_url(request, timeout):
            seen.append((request.full_url, timeout))
            if ":18888/" in request.full_url:
                raise urllib.error.URLError(ConnectionRefusedError())
            return Response()

        opener = Mock()
        opener.open.side_effect = open_url
        with patch.dict(os.environ, {}, clear=True), patch.object(
            scene_model.urllib.request, "build_opener", return_value=opener
        ) as build_opener:
            endpoint = scene_model._loopback_endpoint()

        self.assertEqual(endpoint, "http://127.0.0.1:11434")
        build_opener.assert_called_once()
        proxy_handler = build_opener.call_args.args[0]
        self.assertIsInstance(proxy_handler, scene_model.urllib.request.ProxyHandler)
        self.assertEqual(proxy_handler.proxies, {})
        opener.open.assert_has_calls([
            call(ANY, timeout=1),
            call(ANY, timeout=1),
        ])
        self.assertEqual([url for url, _timeout in seen], [
            "http://127.0.0.1:18888/health",
            "http://127.0.0.1:11434/api/tags",
        ])


if __name__ == "__main__":
    unittest.main()
