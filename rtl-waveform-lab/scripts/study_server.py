#!/usr/bin/env python3
"""Loopback-only static study server with opt-in local PDF analysis."""

from __future__ import annotations

import argparse
from functools import partial
import hashlib
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import pathlib
import secrets
import tempfile
import threading
import time
from urllib.parse import unquote, urlsplit

from pdf_study_analyzer import AnalysisError, PROFILES, analyze_pdf


MAX_PDF_BYTES = 25 * 1024 * 1024
SESSION_TTL_SECONDS = 60
ALLOWED_STATIC_PREFIXES = ("/docs/", "/rtl/", "/tb/", "/build/")


class StudyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], handler: type[SimpleHTTPRequestHandler]):
        super().__init__(server_address, handler)
        self.analysis_lock = threading.Lock()
        self.session_lock = threading.Lock()
        self.sessions: dict[str, float] = {}

    def issue_token(self) -> str:
        now = time.monotonic()
        with self.session_lock:
            self.sessions = {token: expiry for token, expiry in self.sessions.items() if expiry > now}
            token = secrets.token_urlsafe(32)
            self.sessions[token] = now + SESSION_TTL_SECONDS
        return token

    def consume_token(self, token: str) -> bool:
        with self.session_lock:
            expiry = self.sessions.pop(token, 0.0)
        return expiry > time.monotonic()


class StudyRequestHandler(SimpleHTTPRequestHandler):
    server_version = "RTLStudyServer/1"

    @property
    def study_server(self) -> StudyServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, format: str, *args: object) -> None:
        if self.command in {"GET", "HEAD"} and not self.path.startswith("/api/"):
            super().log_message(format, *args)

    def _json_response(self, status: HTTPStatus, code: str, payload: dict[str, object]) -> None:
        body = payload if status is HTTPStatus.OK else {"error": {"code": code, **payload}}
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _expected_origin(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def _trusted_host(self) -> bool:
        return (
            self.client_address[0] in {"127.0.0.1", "::1"}
            and self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"
        )

    def _trusted_post_origin(self) -> bool:
        return self._trusted_host() and self.headers.get("Origin") == self._expected_origin()

    def _static_path_allowed(self) -> bool:
        parsed_path = unquote(urlsplit(self.path).path)
        if parsed_path == "/":
            return True
        if not parsed_path.startswith(ALLOWED_STATIC_PREFIXES):
            return False
        relative = pathlib.PurePosixPath(parsed_path.lstrip("/"))
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            return False
        root = pathlib.Path(self.directory).resolve()
        candidate = root.joinpath(*relative.parts)
        try:
            candidate.resolve(strict=False).relative_to(root)
        except ValueError:
            return False
        current = candidate
        while current != root:
            if current.is_symlink():
                return False
            current = current.parent
        return candidate.is_file()

    def do_GET(self) -> None:  # noqa: N802 - inherited API
        parsed_path = urlsplit(self.path).path
        if not self._trusted_host():
            if parsed_path.startswith("/api/"):
                self._json_response(HTTPStatus.FORBIDDEN, "LOOPBACK_REQUIRED", {"message": "Use the exact 127.0.0.1 study address."})
            else:
                self.send_error(HTTPStatus.FORBIDDEN)
            return
        if parsed_path == "/api/v1/session":
            self._json_response(HTTPStatus.OK, "", {"token": self.study_server.issue_token(), "expiresInSeconds": SESSION_TTL_SECONDS})
            return
        if parsed_path == "/":
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/docs/study.html")
            self.end_headers()
            return
        if not self._static_path_allowed():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802 - inherited API
        if not self._trusted_host():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not self._static_path_allowed():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        super().do_HEAD()

    def do_OPTIONS(self) -> None:  # noqa: N802 - inherited API
        self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

    def do_POST(self) -> None:  # noqa: N802 - inherited API
        parsed_path = urlsplit(self.path).path
        prefix = "/api/v1/pdf-candidates/"
        block = parsed_path.removeprefix(prefix) if parsed_path.startswith(prefix) else ""
        if block not in PROFILES or parsed_path != f"{prefix}{block}":
            self._json_response(HTTPStatus.NOT_FOUND, "UNKNOWN_ROUTE", {"message": "Unknown local API route."})
            return
        if not self._trusted_post_origin():
            self._json_response(HTTPStatus.FORBIDDEN, "LOOPBACK_REQUIRED", {"message": "Use the exact 127.0.0.1 study origin."})
            return
        if not self.study_server.consume_token(self.headers.get("X-Study-Token", "")):
            self._json_response(HTTPStatus.FORBIDDEN, "TOKEN_INVALID", {"message": "Request a fresh one-use analysis token."})
            return
        if self.headers.get("Transfer-Encoding") is not None:
            self._json_response(HTTPStatus.BAD_REQUEST, "UNSUPPORTED_TRANSFER", {"message": "Chunked or encoded request bodies are not accepted."})
            return
        if self.headers.get_content_type() != "application/pdf":
            self._json_response(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "PDF_REQUIRED", {"message": "Only application/pdf is accepted."})
            return
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            self._json_response(HTTPStatus.LENGTH_REQUIRED, "LENGTH_REQUIRED", {"message": "A decimal Content-Length is required."})
            return
        try:
            content_length = int(length_header)
        except ValueError:
            self._json_response(HTTPStatus.BAD_REQUEST, "BAD_LENGTH", {"message": "Content-Length must be a decimal integer."})
            return
        if not 1 <= content_length <= MAX_PDF_BYTES:
            self._json_response(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "PDF_SIZE", {"message": "PDF must be between 1 byte and 25 MiB."})
            return
        if not self.study_server.analysis_lock.acquire(blocking=False):
            self._json_response(HTTPStatus.TOO_MANY_REQUESTS, "ANALYZER_BUSY", {"message": "Another local PDF analysis is running."})
            return

        try:
            self.connection.settimeout(15)
            digest = hashlib.sha256()
            with tempfile.TemporaryDirectory(prefix="rtl-study-") as temporary_directory:
                os.chmod(temporary_directory, 0o700)
                pdf_path = pathlib.Path(temporary_directory) / "input.pdf"
                with pdf_path.open("xb") as handle:
                    os.chmod(pdf_path, 0o600)
                    remaining = content_length
                    while remaining:
                        chunk = self.rfile.read(min(remaining, 1024 * 1024))
                        if not chunk:
                            self._json_response(HTTPStatus.BAD_REQUEST, "UPLOAD_INCOMPLETE", {"message": "The PDF body ended early."})
                            return
                        digest.update(chunk)
                        handle.write(chunk)
                        remaining -= len(chunk)
                with pdf_path.open("rb") as handle:
                    if b"%PDF-" not in handle.read(1024):
                        self._json_response(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "PDF_HEADER", {"message": "The selected file has no PDF header."})
                        return
                try:
                    result = analyze_pdf(pdf_path, "selected.pdf", block)
                except AnalysisError as error:
                    message = str(error)
                    if "required" in message:
                        status, code = HTTPStatus.SERVICE_UNAVAILABLE, "POPPLER_UNAVAILABLE"
                    elif "timeout" in message or "timed out" in message:
                        status, code = HTTPStatus.GATEWAY_TIMEOUT, "ANALYSIS_TIMEOUT"
                    else:
                        status, code = HTTPStatus.UNPROCESSABLE_ENTITY, "PDF_UNREADABLE"
                    self._json_response(status, code, {"message": message})
                    return

            if not all(candidate["available"] for candidate in result["candidates"]):
                self._json_response(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    "INSUFFICIENT_CANDIDATES",
                    {"message": "This PDF does not contain two qualified readings for the selected day. Try another source or attach it to Codex."},
                )
                return
            result.pop("fileName", None)
            result.pop("identitySuggestion", None)
            result.update({
                "schemaVersion": 1,
                "algorithmVersion": "rtl-study-v1",
                "pdfSha256": digest.hexdigest(),
                "extraction": {"tool": "pdftotext", "textOnly": True},
            })
            self._json_response(HTTPStatus.OK, "", result)
        except TimeoutError:
            try:
                self._json_response(HTTPStatus.REQUEST_TIMEOUT, "UPLOAD_TIMEOUT", {"message": "The local PDF upload exceeded 15 seconds."})
            except (BrokenPipeError, ConnectionError):
                pass
        except (BrokenPipeError, ConnectionError):
            pass
        except Exception:
            try:
                self._json_response(HTTPStatus.INTERNAL_SERVER_ERROR, "INTERNAL_ERROR", {"message": "The local analyzer failed safely without retaining the PDF."})
            except (BrokenPipeError, ConnectionError):
                pass
        finally:
            self.study_server.analysis_lock.release()

    def list_directory(self, path: str):  # type: ignore[no-untyped-def]
        self.send_error(HTTPStatus.NOT_FOUND)
        return None

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=4173)
    parser.add_argument("--directory", type=pathlib.Path, default=pathlib.Path.cwd())
    args = parser.parse_args()
    directory = args.directory.resolve()
    handler = partial(StudyRequestHandler, directory=str(directory))
    server = StudyServer(("127.0.0.1", args.port), handler)
    print(f"RTL study page: http://127.0.0.1:{args.port}/docs/study.html", flush=True)
    print("PDF analysis: explicit opt-in, loopback-only, temporary, and never uploaded to the internet.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
