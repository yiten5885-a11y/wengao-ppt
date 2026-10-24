#!/usr/bin/env python3
"""tools/searxng/searxng_service.py

轻量级 SearXNG 兼容本地微服务：
- 监听 127.0.0.1:8890
- 对外提供完全兼容 SearXNG 的 JSON 搜索接口：
    GET /search?q=<query>&format=json&language=<locale>
- 内部采用 DirectSearchBackend 驱动上游稳定检索并格式化输出
- 解决本地 Mac mini 无 Docker 环境下无法启动 SearXNG 容器的问题
"""

from __future__ import annotations

import json
import logging
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from wengao_ppt.research.search.direct import DirectSearchBackend

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("searxng_compat_service")

search_backend = DirectSearchBackend()


class SearxngCompatHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # 简化日志输出
        logger.info("%s - %s", self.address_string(), format % args)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/health", "/status"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "service": "searxng-compat-service", "port": 8890}).encode("utf-8"))
            return

        if parsed.path == "/search":
            qs = urllib.parse.parse_qs(parsed.query)
            q = qs.get("q", [""])[0]
            fmt = qs.get("format", ["json"])[0]
            locale = qs.get("language", ["zh"])[0]

            if not q:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"error": "Missing query parameter q"}')
                return

            items = search_backend.search(q, locale=locale, max_results=10, timeout_s=8)

            # 转换为标准 SearXNG results 格式
            results = []
            for item in items:
                results.append({
                    "title": item.title,
                    "url": item.url,
                    "content": item.snippet,
                    "engine": item.engine or "bing",
                    "parsed_url": [urllib.parse.urlparse(item.url).scheme, urllib.parse.urlparse(item.url).netloc],
                    "score": 1.0,
                })

            response_payload = {
                "query": q,
                "number_of_results": len(results),
                "results": results,
                "answers": [],
                "corrections": [],
                "infoboxes": [],
                "suggestions": [],
                "unresponsive_engines": [],
            }

            resp_bytes = json.dumps(response_payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(resp_bytes)))
            self.end_headers()
            self.wfile.write(resp_bytes)
            return

        self.send_response(404)
        self.end_headers()


def run(host: str = "127.0.0.1", port: int = 8890):
    server = HTTPServer((host, port), SearxngCompatHandler)
    logger.info("SearXNG 兼容服务已在 http://%s:%d 启动...", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("正在停止服务...")
    finally:
        server.server_close()


if __name__ == "__main__":
    p = 8890
    if len(sys.argv) > 1:
        p = int(sys.argv[1])
    run(port=p)
