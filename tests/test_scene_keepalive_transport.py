"""Connection reuse for photo search/download (2026-10-28).

Behind a slow proxy route each new HTTPS connection cost 5-10 s of TLS
handshake, so per-request connections made every scene search time out. These
tests run a real local HTTPS server and count TCP connections.
"""
import datetime
import ipaddress
import json
import ssl
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from wengao_ppt import scene_images as si


def _self_signed(tmp: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp / "cert.pem", tmp / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path


class _Server:
    def __init__(self, tmp: Path):
        self.connections = 0
        self.drop_next_on_reuse = False
        owner = self
        body = json.dumps({"ok": True, "pad": "x" * 2000}).encode()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                owner.connections += 1
                self.requests_here = 0
                super().setup()

            def log_message(self, *args):
                pass

            def do_GET(self):
                self.requests_here += 1
                if owner.drop_next_on_reuse and self.requests_here > 1:
                    owner.drop_next_on_reuse = False
                    self.close_connection = True
                    self.connection.shutdown(2)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        cert, key = _self_signed(tmp)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(cert, key)
        self.httpd.socket = server_ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.client_ctx = ssl.create_default_context(cafile=str(cert))
        self.url = f"https://127.0.0.1:{self.httpd.server_address[1]}/search"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class KeepAliveTransportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.server = _Server(Path(tmp.name))
        self.addCleanup(self.server.stop)
        self.handler = si._KeepAliveHTTPSHandler(context=self.server.client_ctx)
        self.addCleanup(self.handler.close_all)

    def _get(self, *, read_all=True):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), self.handler)
        with opener.open(self.server.url, timeout=5) as resp:
            return json.loads(resp.read()) if read_all else resp.read(10)

    def test_sequential_requests_share_one_connection(self):
        for _ in range(5):
            self.assertTrue(self._get()["ok"])
        self.assertEqual(self.server.connections, 1)

    def test_stale_reused_connection_is_retried_on_a_fresh_one(self):
        self._get()
        self.server.drop_next_on_reuse = True
        self.assertTrue(self._get()["ok"])
        self.assertEqual(self.server.connections, 2)

    def test_partially_read_response_is_not_reused(self):
        self._get(read_all=False)
        self._get()
        self.assertEqual(self.server.connections, 2)

    def test_request_json_and_download_use_the_shared_pool(self):
        built = []
        real_build = urllib.request.build_opener

        def spy(*handlers):
            built.append(handlers)
            return real_build(*handlers)

        from unittest import mock
        with mock.patch.object(si.urllib.request, "build_opener", side_effect=spy), \
                mock.patch.object(si, "_url_is_public_https", return_value=True):
            try:
                si._request_json("https://api.pexels.com/v1/search?query=x",
                                 {"api.pexels.com"}, timeout_s=0.01, max_attempts=1)
            except si.SceneImageError:
                pass
        self.assertTrue(built and any(h is si._KEEPALIVE_HTTPS for h in built[0]))

    def test_time_limits_leave_room_for_a_slow_first_handshake(self):
        for provider in ("pexels", "openverse", "wikimedia-commons"):
            with self.subTest(provider=provider):
                self.assertGreaterEqual(si.SCENE_SEARCH_QUERY_TIMEOUT_BY_PROVIDER[provider], 15.0)
                self.assertGreaterEqual(si.SCENE_SEARCH_BUDGET_BY_PROVIDER[provider],
                                        2 * si.SCENE_SEARCH_QUERY_TIMEOUT_BY_PROVIDER[provider])


class LiveProxyChoiceTests(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        si._proxy_choice = (0.0, {})
        self.addCleanup(setattr, si, "_proxy_choice", (0.0, {}))
        patcher = mock.patch.object(si.urllib.request, "getproxies_environment",
                                    return_value={"https": "http://127.0.0.1:9"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_closed_proxy_port_means_direct(self):
        from unittest import mock
        with mock.patch.object(si.socket, "create_connection", side_effect=OSError("refused")):
            self.assertEqual(si._live_proxy_handler().proxies, {})

    def test_open_proxy_port_is_used(self):
        from unittest import mock

        class _Sock:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch.object(si.socket, "create_connection", return_value=_Sock()):
            self.assertEqual(si._live_proxy_handler().proxies,
                             {"https": "http://127.0.0.1:9"})


class VisionDecidesPrescreenTests(unittest.TestCase):
    """2-of-3 caption anchors earn a visual review only when a reviewer exists."""

    ANCHORS = {"subject": "executives", "action": "discussing strategy",
               "setting": "meeting room"}
    # subject + setting, no action words: 2 of 3
    ROW = {"provider": "pexels", "source_key": "p1", "title":
           "Executives in a modern boardroom", "width": 1600, "height": 1000,
           "download_url": "https://images.pexels.com/p1.jpg",
           "license": "Pexels License", "license_url": "https://www.pexels.com/license/",
           "source_page": "https://www.pexels.com/photo/p1/", "creator": "x"}

    def _run(self, reviewer):
        from unittest import mock

        class _P:
            name = "pexels"
            allowed_hosts = {"images.pexels.com"}

            def search_scene(self, *a, **k):
                return [dict(VisionDecidesPrescreenTests.ROW)]

            search = search_scene

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = si.ScenePhotoProvider(tmp.name, providers=[_P()], visual_reviewer=reviewer)
        ctx = {**self.ANCHORS, "scene_key": "s", "metadata_query":
               "executives discussing strategy meeting room",
               "photo_search_terms": self.ANCHORS}
        reached_download = []
        with mock.patch.object(si, "_is_allowed_license", return_value=True), \
                mock.patch.object(si, "_download_image",
                                  side_effect=lambda *a, **k: reached_download.append(1)
                                  or (_ for _ in ()).throw(
                                      si.SceneImageError("SCENE.IMAGE_FETCH_FAILED", "x"))):
            try:
                p.fetch_one("executives discussing strategy meeting room", scene_context=ctx)
            except si.SceneImageError:
                pass
        codes = [f.get("code") for f in p.errors] if hasattr(p, "errors") else []
        return bool(reached_download), codes

    def test_two_of_three_goes_on_to_review_when_reviewer_present(self):
        reached, _ = self._run(reviewer=lambda scene, data: {"status": "PASSED"})
        self.assertTrue(reached)

    def test_two_of_three_is_still_filtered_without_reviewer(self):
        reached, _ = self._run(reviewer=None)
        self.assertFalse(reached)


class BusinessSceneAliasTests(unittest.TestCase):
    """Real Pexels captions from the 2026-10-28 telecom deck run."""

    CASES = [
        ({"subject": "executives", "action": "discussing strategy", "setting": "meeting room"},
         "Senior executives discussing strategies in a modern boardroom setting.", True),
        ({"subject": "engineer", "action": "inspecting servers", "setting": "data center"},
         "Technician inspecting servers in a datacenter aisle.", True),
        ({"subject": "executives", "action": "discussing strategy", "setting": "meeting room"},
         "Musician recording vocals in a studio.", False),
        ({"subject": "executives", "action": "discussing strategy", "setting": "meeting room"},
         "Executives discussing strategy on a beach.", False),
        ({"subject": "customer", "action": "checking out", "setting": "retail store"},
         "A person using contactless payment at a retail counter in Portugal wearing an apron.",
         True),
        ({"subject": "customer", "action": "checking out", "setting": "retail store"},
         "A woman shops for clothes in a store while wearing a face mask.", False),
    ]

    def test_business_synonyms_accept_matches_and_still_reject_off_topic(self):
        for anchors, title, expected in self.CASES:
            with self.subTest(title=title):
                query = " ".join([anchors["subject"], anchors["action"], anchors["setting"]])
                self.assertEqual(
                    si.scene_metadata_matches_query(
                        query, {"title": title}, scene_context={"photo_search_terms": anchors}),
                    expected)


if __name__ == "__main__":
    unittest.main()
