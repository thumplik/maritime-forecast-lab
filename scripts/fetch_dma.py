"""Download one or more days of Danish Maritime Authority AIS (http://aisdata.ais.dk/).

DMA publishes daily zips for recent years and *monthly* zips (15-22 GB) for 2023 and earlier.
For a monthly zip this script reads only the zip directory and the one day's member with HTTP
range requests, so a single day costs ~0.6 GB instead of the whole month. zipfile verifies the
member CRC32 while extracting.

Output: data/raw/dma/<date>/aisdk-<date>.csv
        data/manifests/dma_fetch.json   (url, member, sizes, CRC, SHA-256 per day; committed)

Usage:
    uv run --project backend python scripts/fetch_dma.py 2023-06-01 [2023-06-02 ...]
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import sys
import zipfile
from datetime import UTC, date, datetime

import httpx

from maritime_forecast.config import MANIFEST_DIR, RAW_DIR

BUCKET = "http://aisdata.ais.dk.s3.eu-central-1.amazonaws.com"


class HTTPRangeFile(io.RawIOBase):
    """Read-only, seekable view of a remote file via HTTP Range requests."""

    def __init__(self, client: httpx.Client, url: str):
        self.client, self.url, self.pos = client, url, 0
        r = client.head(url)
        r.raise_for_status()
        if r.headers.get("accept-ranges") != "bytes":
            raise RuntimeError(f"{url} does not support range requests")
        self.size = int(r.headers["content-length"])

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.pos, io.SEEK_END: self.size}[whence]
        self.pos = base + offset
        return self.pos

    def readinto(self, b) -> int:
        if self.pos >= self.size:
            return 0
        end = min(self.pos + len(b), self.size) - 1
        for attempt in range(4):
            try:
                r = self.client.get(self.url, headers={"Range": f"bytes={self.pos}-{end}"})
                r.raise_for_status()
                break
            except httpx.HTTPError:
                if attempt == 3:
                    raise
        data = r.content
        b[: len(data)] = data
        self.pos += len(data)
        return len(data)


def candidates(d: date) -> list[tuple[str, str]]:
    """(zip URL, member name) in order of preference."""
    day, month = f"aisdk-{d.isoformat()}", f"aisdk-{d:%Y-%m}"
    return [
        (f"{BUCKET}/{d.year}/{day}.zip", f"{day}.csv"),
        (f"{BUCKET}/{day}.zip", f"{day}.csv"),
        (f"{BUCKET}/{d.year}/{month}.zip", f"{day}.csv"),
    ]


def fetch_day(client: httpx.Client, d: date) -> dict:
    for url, member in candidates(d):
        if client.head(url).status_code != 200:
            continue
        remote = io.BufferedReader(HTTPRangeFile(client, url), buffer_size=8 << 20)
        with zipfile.ZipFile(remote) as zf:
            info = zf.getinfo(member)
            out_dir = RAW_DIR / "dma" / d.isoformat()
            out_dir.mkdir(parents=True, exist_ok=True)
            out = out_dir / member
            tmp = out.with_suffix(".csv.part")
            h = hashlib.sha256()
            with zf.open(info) as src, open(tmp, "wb") as dst:  # CRC checked by zipfile
                for chunk in iter(lambda: src.read(8 << 20), b""):
                    dst.write(chunk)
                    h.update(chunk)
            tmp.replace(out)
        return {
            "date": d.isoformat(),
            "url": url,
            "member": member,
            "compressed_bytes": info.compress_size,
            "bytes": info.file_size,
            "crc32": f"{info.CRC:#010x}",
            "sha256": h.hexdigest(),
            "output": str(out.relative_to(RAW_DIR.parent.parent)),
            "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
    raise FileNotFoundError(f"no DMA file found for {d} (tried {[u for u, _ in candidates(d)]})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("dates", nargs="+", type=date.fromisoformat)
    args = ap.parse_args()

    manifest_path = MANIFEST_DIR / "dma_fetch.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {
        "source": "Danish Maritime Authority historical AIS",
        "index": "http://aisdata.ais.dk/",
        "license_note": "DMA AIS data terms apply; see http://aisdata.ais.dk/ and dma.dk.",
        "files": {},
    }  # fmt: skip
    if shutil.disk_usage(RAW_DIR).free < 5 * len(args.dates) << 30:
        sys.exit("need ~5 GB free per day (2-3 GB CSV each)")
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        for d in args.dates:
            rec = fetch_day(client, d)
            manifest["files"][rec["date"]] = rec
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            print(f"ok {d}: {rec['bytes']:,} bytes, sha256 {rec['sha256'][:12]}… from {rec['url']}")


if __name__ == "__main__":
    main()
