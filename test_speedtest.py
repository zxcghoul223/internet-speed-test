"""Local HTTP integration tests; no external network access is required."""

from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

import speedtest


BODY = bytes(range(256)) * 513 + b"last chunk"
DELAY = 0.02


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        with self.server.lock:
            self.server.starts.append(time.perf_counter())
        if self.path == "/error":
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header("Connection", "close")
        if self.path == "/chunked":
            self.send_header("Transfer-Encoding", "chunked")
        elif self.path != "/unknown":
            length = 0 if self.path == "/empty" else len(BODY)
            self.send_header("Content-Length", str(length))
        self.end_headers()
        self.close_connection = True

        try:
            if self.path == "/empty":
                return
            if self.path == "/timeout":
                time.sleep(0.2)
                return
            if self.path == "/truncated":
                self.wfile.write(BODY[:100])
                return

            chunks = (BODY[:-10], BODY[-10:])
            for index, chunk in enumerate(chunks):
                if index:
                    time.sleep(DELAY)
                    # Record before the final write: a correctly sequential
                    # client need not wait for the handler thread to finish.
                    with self.server.lock:
                        self.server.final_chunks.append(time.perf_counter())
                if self.path == "/chunked":
                    self.wfile.write(f"{len(chunk):x}\r\n".encode())
                    self.wfile.write(chunk + b"\r\n")
                else:
                    self.wfile.write(chunk)
                self.wfile.flush()
            if self.path == "/chunked":
                self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass  # Expected when the client times out or rejects an answer.


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.lock = threading.Lock()
        self.server.starts = []
        self.server.final_chunks = []
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def url(self, path):
        return f"http://127.0.0.1:{self.server.server_port}{path}"

    def test_ten_complete_sequential_responses_include_body_time(self):
        downloaded, elapsed = speedtest.measure(self.url("/sized"))
        self.assertEqual(downloaded, 10 * len(BODY))
        self.assertEqual(len(self.server.starts), 10)
        self.assertEqual(len(self.server.final_chunks), 10)
        self.assertGreaterEqual(elapsed, 10 * DELAY * 0.9)
        for next_start, previous_end in zip(
            self.server.starts[1:], self.server.final_chunks
        ):
            self.assertGreaterEqual(next_start, previous_end)

    def test_response_without_content_length(self):
        downloaded, elapsed = speedtest.measure(self.url("/unknown"))
        self.assertEqual(downloaded, 10 * len(BODY))
        self.assertGreater(elapsed, 0)
        self.assertEqual(len(self.server.starts), 10)

    def test_chunked_response_counts_only_payload(self):
        downloaded, _ = speedtest.measure(self.url("/chunked"))
        self.assertEqual(downloaded, 10 * len(BODY))
        self.assertEqual(len(self.server.starts), 10)

    def test_empty_responses(self):
        downloaded, elapsed = speedtest.measure(self.url("/empty"))
        self.assertEqual(downloaded, 0)
        self.assertGreater(elapsed, 0)
        self.assertEqual(len(self.server.starts), 10)

    def test_truncated_content_length_stops_series(self):
        with self.assertRaisesRegex(RuntimeError, r"Request 1/10: incomplete"):
            speedtest.measure(self.url("/truncated"))
        self.assertEqual(len(self.server.starts), 1)

    def test_http_error_stops_series(self):
        with self.assertRaisesRegex(RuntimeError, r"Request 1/10: HTTP 404"):
            speedtest.measure(self.url("/error"))
        self.assertEqual(len(self.server.starts), 1)

    def test_body_timeout_stops_series(self):
        with patch.object(speedtest, "TIMEOUT", 0.05):
            with self.assertRaisesRegex(RuntimeError, r"Request 1/10:"):
                speedtest.measure(self.url("/timeout"))
        self.assertEqual(len(self.server.starts), 1)


class CLITests(unittest.TestCase):
    def run_main(self, result=None, error=None):
        output, errors = StringIO(), StringIO()
        with patch.object(sys, "argv", ["speedtest.py", "https://example.com/file"]):
            with patch.object(speedtest, "measure", return_value=result, side_effect=error):
                with redirect_stdout(output), redirect_stderr(errors):
                    code = speedtest.main()
        return code, output.getvalue(), errors.getvalue()

    def test_average_and_megabits_per_second(self):
        code, output, errors = self.run_main((10_000_000, 2.5))
        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        self.assertIn("Requests: 10\n", output)
        self.assertIn("Average request time: 0.250000 s\n", output)
        self.assertIn("Total downloaded: 10000000 bytes\n", output)
        self.assertIn("Download speed: 32.000000 Mbit/s\n", output)

    def test_zero_bytes_have_zero_speed(self):
        code, output, _ = self.run_main((0, 1.0))
        self.assertEqual(code, 0)
        self.assertIn("Download speed: 0.000000 Mbit/s", output)

    def test_failure_does_not_print_successful_measurement(self):
        code, output, errors = self.run_main(error=RuntimeError("Request 2/10: failed"))
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("Error: Request 2/10: failed", errors)

    def test_invalid_urls_exit_two_without_traceback(self):
        script = Path(speedtest.__file__)
        for url in ("relative/path", "file:///tmp/file", "https:///missing-host", "http://localhost:bad"):
            with self.subTest(url=url):
                result = subprocess.run(
                    [sys.executable, str(script), url],
                    capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("error:", result.stderr)
                self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
