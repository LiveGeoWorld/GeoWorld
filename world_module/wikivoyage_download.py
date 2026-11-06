"""Acquire the official source dump used by the existing city ingestor."""

import hashlib
import re
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from urllib.parse import urljoin
from urllib.request import Request, urlopen

BASE = "https://dumps.wikimedia.org/enwikivoyage/latest/"
DUMP_NAME = "enwikivoyage-latest-pages-articles.xml.bz2"
_download_lock = Lock()


def _open_source(name):
    request = Request(urljoin(BASE, name), headers={"User-Agent": "GeoWorld World Studio dump downloader"})
    return urlopen(request, timeout=60)


def ensure_wikivoyage_dump(target: Path) -> Path:
    with _download_lock:
        if target.is_file():
            return target
        with _open_source("enwikivoyage-latest-sha1sums.txt") as response:
            checksums = {}
            for line in response.read().decode("ascii").splitlines():
                if line.strip():
                    checksum, name = line.split(maxsplit=1)
                    checksums[name.lstrip("*")] = checksum
        articles = [(name, checksum) for name, checksum in checksums.items()
                    if re.fullmatch(r"enwikivoyage-(?:latest|\d{8})-pages-articles\.xml\.bz2", name)]
        if len(articles) != 1:
            raise ValueError("Wikimedia checksum list must contain exactly one articles dump")
        filename, expected = articles[0]
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            digest = hashlib.sha1()
            with NamedTemporaryFile(dir=target.parent, suffix=".part", delete=False) as stream:
                temporary = Path(stream.name)
                with _open_source(DUMP_NAME if filename == DUMP_NAME else
                                  f"../{filename.split('-')[1]}/{filename}") as response:
                    while block := response.read(1024 * 1024):
                        stream.write(block)
                        digest.update(block)
            if digest.hexdigest() != expected:
                raise ValueError("Wikivoyage dump checksum mismatch")
            temporary.replace(target)
            return target
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
