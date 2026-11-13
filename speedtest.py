"""Measure download speed with ten sequential HTTP(S) requests."""

import argparse
from http.client import HTTPException
import sys
from time import perf_counter
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


REQUEST_COUNT = 10
CHUNK_SIZE = 64 * 1024
TIMEOUT = 30


def http_url(value: str) -> str:
    """Validate the URL before making any requests."""
    try:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("use an absolute HTTP or HTTPS URL")
        _ = parts.port  # Also reject invalid port numbers.
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    return value


def measure(url: str) -> tuple[int, float]:
    """Return total body bytes and total request time in seconds."""
    total_bytes = 0
    total_seconds = 0.0

    for number in range(1, REQUEST_COUNT + 1):
        request = Request(url, headers={
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
        })
        downloaded = 0
        try:
            started = perf_counter()
            with urlopen(request, timeout=TIMEOUT) as response:
                if urlsplit(response.geturl()).scheme not in ("http", "https"):
                    raise ValueError("redirect to a non-HTTP(S) URL")
                while chunk := response.read(CHUNK_SIZE):
                    downloaded += len(chunk)
                elapsed = perf_counter() - started

                # A sized read may silently reach EOF before Content-Length.
                expected = response.getheader("Content-Length")
                if expected is not None and not response.getheader("Transfer-Encoding"):
                    if downloaded != int(expected):
                        raise OSError("incomplete response body")
        except HTTPError as error:
            error.close()
            raise RuntimeError(f"Request {number}/{REQUEST_COUNT}: HTTP {error.code}") from error
        except (OSError, HTTPException, ValueError) as error:
            raise RuntimeError(f"Request {number}/{REQUEST_COUNT}: {error}") from error

        total_bytes += downloaded
        total_seconds += elapsed

    return total_bytes, total_seconds


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure download speed with 10 sequential HTTP(S) requests."
    )
    parser.add_argument("url", type=http_url, help="URL of the file to download")
    args = parser.parse_args()

    try:
        total_bytes, total_seconds = measure(args.url)
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Measurement cancelled.", file=sys.stderr)
        return 130

    if total_seconds <= 0:
        print("Error: measured duration is zero.", file=sys.stderr)
        return 1

    print(f"Requests: {REQUEST_COUNT}")
    print(f"Average request time: {total_seconds / REQUEST_COUNT:.6f} s")
    print(f"Total downloaded: {total_bytes} bytes")
    print(f"Download speed: {total_bytes * 8 / total_seconds / 1_000_000:.6f} Mbit/s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
