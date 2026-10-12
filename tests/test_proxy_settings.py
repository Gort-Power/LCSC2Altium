"""Proxy mode configuration for outbound EasyEDA requests."""

import pytest
import requests

from lcsc2altium.easyeda_client import EasyedaAccessDeniedError, EasyedaClient


def test_direct_connection_ignores_system_proxy_settings():
    client = EasyedaClient(proxy_mode="direct")
    try:
        assert client._session.trust_env is False
        assert client._session.proxies == {}
    finally:
        client.close()


def test_system_proxy_mode_uses_environment_and_system_proxy_settings():
    client = EasyedaClient(proxy_mode="system")
    try:
        assert client._session.trust_env is True
    finally:
        client.close()


def test_connection_check_uses_component_api(monkeypatch):
    client = EasyedaClient(proxy_mode="direct")
    checked_parts = []
    monkeypatch.setattr(
        client,
        "fetch_component",
        lambda lcsc: checked_parts.append(lcsc) or {"result": {}},
    )
    try:
        client.check_connection()
        assert checked_parts == ["C25804"]
    finally:
        client.close()


def test_manual_proxy_uses_address_and_port_for_http_and_https():
    client = EasyedaClient(
        proxy_mode="manual",
        proxy_address="proxy.example",
        proxy_port=3128,
    )
    try:
        assert client._session.trust_env is False
        assert client._session.proxies == {
            "http": "http://proxy.example:3128",
            "https": "http://proxy.example:3128",
        }
    finally:
        client.close()


def test_proxy_connection_error_reports_proxy_host_and_port(monkeypatch):
    client = EasyedaClient(
        retries=3,
        proxy_mode="manual",
        proxy_address="127.0.0.1",
        proxy_port=18080,
    )
    calls = []

    def fail_request(*args, **kwargs):
        calls.append((args, kwargs))
        raise requests.exceptions.ProxyError("connection refused")

    monkeypatch.setattr(client._session, "get", fail_request)
    try:
        with pytest.raises(
            RuntimeError,
            match=r"Cannot connect to proxy at 127\.0\.0\.1:18080",
        ):
            client._get("https://easyeda.com/api/test")
        assert len(calls) == 1
    finally:
        client.close()


def test_http_403_is_not_retried(monkeypatch):
    client = EasyedaClient(retries=3, proxy_mode="direct")
    calls = []
    response = requests.Response()
    response.status_code = 403
    response.url = "https://easyeda.com/api/test"

    def deny_request(*args, **kwargs):
        calls.append((args, kwargs))
        return response

    monkeypatch.setattr(client._session, "get", deny_request)
    try:
        with pytest.raises(EasyedaAccessDeniedError, match="HTTP 403"):
            client._get("https://easyeda.com/api/test")
        assert len(calls) == 1
    finally:
        client.close()


@pytest.mark.parametrize(
    ("address", "port"),
    [
        ("", 8080),
        ("proxy.example:3128", 8080),
        ("http://proxy.example", 8080),
        ("proxy.example", 0),
        ("proxy.example", 65536),
    ],
)
def test_manual_proxy_rejects_invalid_address_or_port(address, port):
    with pytest.raises(ValueError):
        EasyedaClient(
            proxy_mode="manual",
            proxy_address=address,
            proxy_port=port,
        )
