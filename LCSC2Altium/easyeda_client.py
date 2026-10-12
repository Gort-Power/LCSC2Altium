"""HTTP client for the public EasyEDA / LCSC endpoints used by this tool.

Two flavours of data are retrieved:

* ``fetch_component`` returns the raw JSON for one LCSC part number. The ``result``
  member carries the schematic symbol (``dataStr``) and, when the part has one, the
  matching footprint under ``packageDetail``.
* ``download_step`` pulls the AP214 STEP payload that backs the footprint's 3D body.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import zipfile
from typing import Any
from urllib.parse import urlsplit

import requests

from .constants import (
    COMPONENT_URL,
    SEARCH_URL,
    STEP_BASE_URL,
    USER_AGENT,
    WRL_BASE_URL,
)

log = logging.getLogger(__name__)


class EasyedaError(RuntimeError):
    """Raised when the remote API cannot satisfy a request."""


class EasyedaAccessDeniedError(EasyedaError):
    """Raised when EasyEDA denies access to an API endpoint."""


class EasyedaClient:
    """Small, synchronous wrapper around the EasyEDA REST API."""

    def __init__(
        self,
        timeout: float = 30.0,
        retries: int = 2,
        *,
        proxy_mode: str = "system",
        proxy_address: str = "",
        proxy_port: int = 8080,
    ) -> None:
        self.timeout = timeout
        self.retries = max(0, retries)
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://easyeda.com/",
            "Accept-Language": "en-US,en;q=0.9",
        })
        if proxy_mode == "direct":
            self._session.trust_env = False
        elif proxy_mode == "system":
            self._session.trust_env = True
        elif proxy_mode == "manual":
            address = proxy_address.strip()
            parsed = urlsplit(f"//{address}")
            if (
                not address
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
                or any(char.isspace() for char in address)
            ):
                raise ValueError("Proxy address must be a hostname or IP without a port.")
            if not 1 <= proxy_port <= 65535:
                raise ValueError("Proxy port must be between 1 and 65535.")
            host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
            proxy_url = f"http://{host}:{proxy_port}"
            self._session.trust_env = False
            self._session.proxies.update({"http": proxy_url, "https": proxy_url})
        else:
            raise ValueError(f"Unknown proxy mode: {proxy_mode}")

    # ---------------------------------------------------------------- low level
    def _get(self, url: str, *, binary: bool = False) -> requests.Response:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self._session.get(url, timeout=self.timeout)
                if resp.status_code == 200:
                    return resp
                if resp.status_code == 403:
                    raise EasyedaAccessDeniedError(
                        f"HTTP 403 for {url}. EasyEDA is blocked; check VPN or network access."
                    )
                else:
                    last = EasyedaError(f"HTTP {resp.status_code} for {url}")
            except requests.exceptions.ProxyError as exc:
                scheme = urlsplit(url).scheme
                proxy_url = self._session.proxies.get(scheme) or self._session.proxies.get("all")
                if proxy_url:
                    proxy = urlsplit(proxy_url)
                    endpoint = proxy.hostname or "configured proxy"
                    if proxy.port is not None:
                        endpoint = f"{endpoint}:{proxy.port}"
                    message = (
                        f"Cannot connect to proxy at {endpoint}. "
                        "Check that the proxy application is running and that its "
                        "address and port are correct."
                    )
                else:
                    message = (
                        "Cannot connect through the system proxy. Check the system "
                        "proxy settings and ensure the proxy is running."
                    )
                raise EasyedaError(message) from exc
            except requests.RequestException as exc:  # network / timeout
                last = exc
            if attempt < self.retries:
                continue
        raise EasyedaError(str(last) if last else f"Request failed: {url}")

    def _get_json(self, url: str) -> dict[str, Any]:
        resp = self._get(url)
        try:
            data = resp.json()
        except ValueError as exc:
            raise EasyedaError(f"Malformed JSON from {url}") from exc
        if isinstance(data, dict) and data.get("success") is False:
            raise EasyedaError(data.get("message") or "EasyEDA reported failure")
        return data

    # ---------------------------------------------------------------- component
    def fetch_component(self, lcsc: str) -> dict[str, Any]:
        """Return the ``result`` mapping for *lcsc* (e.g. ``"C25804"``)."""
        lcsc = lcsc.strip().upper()
        url = COMPONENT_URL.format(lcsc=lcsc)
        data = self._get_json(url)
        result = data.get("result")
        if not isinstance(result, dict):
            raise EasyedaError(f"No component data returned for {lcsc}")
        return result

    def check_connection(self) -> None:
        """Verify access to the same EasyEDA API used to fetch components."""
        self.fetch_component("C25804")

    def fetch_search(self, keyword: str) -> list[dict[str, Any]]:
        """Return raw search hits (used only to enrich manufacturer / stock data)."""
        try:
            data = self._get_json(SEARCH_URL.format(lcsc=keyword.strip().upper()))
        except EasyedaError:
            return []
        result = data.get("result")
        if not isinstance(result, dict):
            return []
        products = result.get("productList")
        return products if isinstance(products, list) else []

    def find_exact(self, keyword: str, lcsc: str) -> dict[str, Any] | None:
        """Locate the search entry whose ``number`` matches *lcsc* exactly."""
        wanted = lcsc.strip().upper()
        for item in self.fetch_search(keyword):
            if str(item.get("number", "")).upper() == wanted:
                return item
        return None

    @staticmethod
    def _is_step_payload(data: bytes) -> bool:
        if not data:
            return False
        if data[:2] == b"\x1f\x8b":
            try:
                return EasyedaClient._is_step_payload(gzip.decompress(data))
            except OSError:
                return False
        if data[:4] == b"PK\x03\x04":
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    for name in archive.namelist():
                        lower = name.lower()
                        if lower.endswith(".step") or lower.endswith(".stp"):
                            return EasyedaClient._is_step_payload(archive.read(name))
            except (zipfile.BadZipFile, OSError):
                return False
            return False
        head = data[:256].lstrip(b"\x00\t\r\n \xef\xbb\xbf")
        text = head.decode("utf-8", errors="ignore")
        return (
            "ISO-10303-21" in text
            or "ISO 10303" in text
            or text.lstrip().startswith("FILE;")
        )

    # ---------------------------------------------------------------- 3D models
    def download_step(self, model_uuid: str) -> bytes:
        """Download the STEP (AP214) payload for a 3D model uuid."""
        if not model_uuid:
            raise EasyedaError("Empty 3D model uuid")

        urls = [
            model_uuid if "://" in model_uuid else STEP_BASE_URL.format(uuid=model_uuid),
            ("https://modules.easyeda.com/3dmodel/{uuid}" if "://" not in model_uuid else model_uuid),
        ]
        if model_uuid and "://" not in model_uuid:
            urls = [
                STEP_BASE_URL.format(uuid=model_uuid),
                "https://modules.easyeda.com/3dmodel/{uuid}".format(uuid=model_uuid),
            ]

        last_error: EasyedaError | None = None
        for url in urls:
            try:
                resp = self._get(url, binary=True)
            except EasyedaError as exc:
                last_error = exc
                continue
            data = resp.content
            if not data:
                continue
            if data[:2] == b"\x1f\x8b":
                try:
                    data = gzip.decompress(data)
                except OSError:
                    pass
            if data[:4] == b"PK\x03\x04":
                try:
                    with zipfile.ZipFile(io.BytesIO(data)) as archive:
                        for name in archive.namelist():
                            lower = name.lower()
                            if lower.endswith(".step") or lower.endswith(".stp"):
                                data = archive.read(name)
                                break
                        else:
                            data = b""
                except (zipfile.BadZipFile, OSError):
                    data = b""
            if self._is_step_payload(data):
                return data
            last_error = EasyedaError(f"Downloaded payload from {url} is not a STEP file")

        if last_error is not None:
            raise last_error
        raise EasyedaError("Downloaded payload is not a STEP file")

    def download_wrl(self, model_uuid: str) -> bytes | None:
        """Best-effort WRL download; returns ``None`` when unavailable."""
        if not model_uuid:
            return None
        try:
            resp = self._get(WRL_BASE_URL.format(uuid=model_uuid), binary=True)
        except EasyedaError:
            return None
        return resp.content or None

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "EasyedaClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def extract_data_str(node: Any) -> dict[str, Any]:
    """Normalise a ``dataStr`` field that may be a JSON string or an object."""
    if isinstance(node, str):
        try:
            parsed = json.loads(node)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return node if isinstance(node, dict) else {}
