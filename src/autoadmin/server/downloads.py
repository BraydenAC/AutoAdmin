"""Checksum-keyed download cache with bounded retries.

Files with a known checksum are stored as cache/<algo>/<hash> and reused by any
world or retry. Files without one are keyed by URL hash.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

USER_AGENT = "AutoAdmin/0.1 (+https://github.com/BraydenAC/AutoAdmin)"
RETRIES = 3
CHUNK = 1 << 20


class DownloadError(Exception):
    pass


class NotFound(DownloadError):
    """HTTP 404: retrying won't help."""


def _final(e: Exception) -> Exception | None:
    """Client errors (4xx) are permanent; return the error to raise immediately."""
    if isinstance(e, urllib.error.HTTPError) and 400 <= e.code < 500:
        return NotFound(str(e)) if e.code == 404 else DownloadError(str(e))
    return None


def _http_get(url: str, timeout: float = 60) -> urllib.request.addinfourl:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout)


def get_json_sync(url: str) -> object:
    with _http_get(url, timeout=30) as r:
        return json.load(r)


async def get_json(url: str) -> object:
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            return await asyncio.to_thread(get_json_sync, url)
        except Exception as e:  # network errors, HTTP errors, bad JSON
            if final := _final(e):
                raise final
            last = e
            await asyncio.sleep(2 ** attempt)
    raise DownloadError(f"GET {url} failed after {RETRIES} tries: {last}")


async def get_text(url: str) -> str:
    def fetch() -> str:
        with _http_get(url, timeout=30) as r:
            return r.read().decode()
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            return await asyncio.to_thread(fetch)
        except Exception as e:
            if final := _final(e):
                raise final
            last = e
            await asyncio.sleep(2 ** attempt)
    raise DownloadError(f"GET {url} failed after {RETRIES} tries: {last}")


class Cache:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _target(self, url: str, algo: str | None, digest: str | None) -> Path:
        if algo and digest:
            return self.root / algo / digest.lower()
        return self.root / "url" / hashlib.sha256(url.encode()).hexdigest()

    def _download_sync(self, url: str, dest: Path, algo: str | None, digest: str | None) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        h = hashlib.new(algo) if algo else None
        with _http_get(url) as r, tmp.open("wb") as f:
            while chunk := r.read(CHUNK):
                f.write(chunk)
                if h:
                    h.update(chunk)
        if h and digest and h.hexdigest().lower() != digest.lower():
            tmp.unlink(missing_ok=True)
            raise DownloadError(f"checksum mismatch for {url}")
        os.replace(tmp, dest)

    async def fetch(self, url: str, *, algo: str | None = None, digest: str | None = None) -> Path:
        """Return a cached path for `url`, downloading it if needed."""
        dest = self._target(url, algo, digest)
        if dest.exists():
            return dest
        last: Exception | None = None
        for attempt in range(RETRIES):
            try:
                await asyncio.to_thread(self._download_sync, url, dest, algo, digest)
                return dest
            except Exception as e:
                if final := _final(e):
                    raise final
                last = e
                log.warning("download %s failed (attempt %d): %s", url, attempt + 1, e)
                await asyncio.sleep(2 ** attempt)
        raise DownloadError(f"download failed after {RETRIES} tries: {url}: {last}")

    async def fetch_to(self, url: str, dest: Path, **kw) -> Path:
        """Fetch through the cache, then copy to `dest`."""
        src = await self.fetch(url, **kw)
        dest.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(shutil.copyfile, src, dest)
        return dest
