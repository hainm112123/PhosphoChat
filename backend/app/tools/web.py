"""Fetch a URL and turn it into clean Markdown for a source note.

Captured pages are untrusted: only http(s), no private/loopback/link-local targets (checked on
every redirect hop), a size cap, and the text is stored as data, never shown to the model as
instructions.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura

MAX_BYTES = 5_000_000
MAX_REDIRECTS = 5
USER_AGENT = "PhosphoChat/0.1 (+personal research wiki)"


class FetchError(ValueError):
    pass


@dataclass
class Fetched:
    url: str
    title: str
    markdown: str
    content_type: str


async def _check_host(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise FetchError("Only http(s) URLs can be captured.")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, None)
    except socket.gaierror as e:
        raise FetchError(f"Cannot resolve {parsed.hostname}.") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise FetchError(f"Refusing to fetch a private or local address ({parsed.hostname}).")


async def fetch_url(url: str) -> Fetched:
    url = url.strip()
    async with httpx.AsyncClient(follow_redirects=False, timeout=20,
                                 headers={"User-Agent": USER_AGENT}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            await _check_host(url)
            async with client.stream("GET", url) as resp:
                if resp.is_redirect:
                    url = urljoin(url, resp.headers.get("location", ""))
                    continue
                if resp.status_code >= 400:
                    raise FetchError(f"{url} returned HTTP {resp.status_code}.")
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise FetchError("Page is larger than 5 MB.")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                encoding = resp.encoding or "utf-8"
                break
        else:
            raise FetchError("Too many redirects.")

    if ctype in ("text/plain", "text/markdown"):
        text = raw.decode(encoding, errors="replace")
        return Fetched(url, urlparse(url).path.rsplit("/", 1)[-1] or url, text, "article")
    if ctype == "application/pdf":
        from app.vault.store import extract_text
        text, _ = extract_text("download.pdf", raw)
        return Fetched(url, urlparse(url).path.rsplit("/", 1)[-1] or url, text, "paper")
    if "html" not in ctype and ctype:
        raise FetchError(f"Unsupported content type '{ctype}'.")

    html = raw.decode(encoding, errors="replace")
    md = trafilatura.extract(html, url=url, output_format="markdown", include_links=True,
                             include_tables=True, favor_recall=True)
    if not md or not md.strip():
        raise FetchError("Could not extract readable text from the page.")
    meta = trafilatura.extract_metadata(html, default_url=url)
    title = (meta.title if meta and meta.title else None) or url
    return Fetched(url, title.strip(), md.strip(), "article")
