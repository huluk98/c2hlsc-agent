#!/usr/bin/env python3
"""Loopback API, token, static-serving, and real-PDF integration checks."""

from __future__ import annotations

from functools import partial
import http.client
import json
import pathlib
import sys
import tempfile
import threading
import unittest


SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
LAB_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from study_server import StudyRequestHandler, StudyServer  # noqa: E402


class StudyServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory(prefix="rtl-server-test-")
        root = pathlib.Path(cls.temp.name)
        (root / "docs").mkdir()
        (root / "docs" / "ok.txt").write_text("ok", encoding="utf-8")
        handler = partial(StudyRequestHandler, directory=str(root))
        cls.server = StudyServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.temp.cleanup()

    def connection(self) -> http.client.HTTPConnection:
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=40)

    def token(self) -> str:
        connection = self.connection()
        connection.request("GET", "/api/v1/session")
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        self.assertEqual(response.status, 200)
        return payload["token"]

    def post(self, path: str, body: bytes, token: str, content_type: str = "application/pdf", origin: str | None = None):
        connection = self.connection()
        headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "Origin": origin or f"http://127.0.0.1:{self.port}",
            "X-Study-Token": token,
        }
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        return response.status, payload

    def test_static_allowlist_and_directory_listing_denial(self) -> None:
        connection = self.connection()
        connection.request("GET", "/docs/ok.txt")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        self.assertEqual(response.read(), b"ok")
        connection.close()

        connection = self.connection()
        connection.request("GET", "/docs/")
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 404)

        connection = self.connection()
        connection.putrequest("GET", "/docs/ok.txt", skip_host=True)
        connection.putheader("Host", "attacker.example")
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 403)

    def test_origin_and_one_use_token_fail_closed(self) -> None:
        token = self.token()
        status, payload = self.post(
            "/api/v1/pdf-candidates/day1", b"%PDF-fake", token,
            origin="http://localhost:4173",
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "LOOPBACK_REQUIRED")

        token = self.token()
        status, payload = self.post(
            "/api/v1/pdf-candidates/day1", b"%PDF-fake", token,
            content_type="text/plain",
        )
        self.assertEqual(status, 415)
        self.assertEqual(payload["error"]["code"], "PDF_REQUIRED")
        status, payload = self.post("/api/v1/pdf-candidates/day1", b"%PDF-fake", token)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "TOKEN_INVALID")

    def test_real_hlstrans_pdf_returns_two_bounded_candidates(self) -> None:
        pdf_path = LAB_ROOT.parent / "papers" / "2507.04315v3.pdf"
        body = pdf_path.read_bytes()
        status, payload = self.post("/api/v1/pdf-candidates/day3", body, self.token())
        self.assertEqual(status, 200)
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(payload["algorithmVersion"], "rtl-study-v1")
        self.assertEqual(len(payload["candidates"]), 2)
        self.assertEqual([item["viewerPage"] for item in payload["candidates"]], [1, 2])
        self.assertTrue(all(len(item["snippet"]) <= 520 for item in payload["candidates"]))
        self.assertNotIn("fileName", payload)
        self.assertRegex(payload["pdfSha256"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
