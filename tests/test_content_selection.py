"""Content check-box selection logic (no Qt widgets required)."""

from unittest.mock import MagicMock

import pytest

from lcsc2altium.builders.library import ComponentEntry
from lcsc2altium.easyeda_client import EasyedaAccessDeniedError
from lcsc2altium.models import ComponentInfo, FootprintData, StepModelRef, SymbolData
from lcsc2altium.ui.main_window import (
    CONTENT_FOOTPRINTS,
    CONTENT_ITEMS,
    CONTENT_MODELS,
    CONTENT_SYMBOLS,
    DEFAULT_CONTENT,
    ConnectionTestWorker,
    DownloadWorker,
    content_selection,
    merge_component_update,
)


def test_default_ticks_symbols_and_footprints():
    assert set(DEFAULT_CONTENT) == {CONTENT_SYMBOLS, CONTENT_FOOTPRINTS}


def test_nothing_ticked_yields_empty_selection():
    assert content_selection(False, False, False) == set()


def test_single_selections():
    assert content_selection(True, False, False) == {CONTENT_SYMBOLS}
    assert content_selection(False, True, False) == {CONTENT_FOOTPRINTS}
    assert content_selection(False, False, True) == {CONTENT_FOOTPRINTS, CONTENT_MODELS}


def test_symbols_and_footprints_together():
    selected = content_selection(True, True, False)
    assert selected == {CONTENT_SYMBOLS, CONTENT_FOOTPRINTS}


def test_all_three_together():
    selected = content_selection(True, True, True)
    assert selected == {CONTENT_SYMBOLS, CONTENT_FOOTPRINTS, CONTENT_MODELS}


def test_models_imply_footprints():
    # A 3D body is embedded in a footprint, so ticking models forces footprints on.
    assert CONTENT_FOOTPRINTS in content_selection(False, False, True)
    assert CONTENT_FOOTPRINTS in content_selection(True, False, True)
    assert CONTENT_MODELS not in content_selection(True, True, False)


def test_items_are_labelled_for_the_user():
    labels = [label for _key, label in CONTENT_ITEMS]
    assert labels == ["Symbols", "Footprints", "3D models"]


@pytest.mark.parametrize("symbols,footprints,models", [
    (False, False, False),
    (True, False, False),
    (False, True, False),
    (False, False, True),
    (True, True, False),
    (True, False, True),
    (True, True, True),
])
def test_selection_is_idempotent(symbols, footprints, models):
    once = content_selection(symbols, footprints, models)
    assert content_selection(symbols, footprints, models) == once


def test_download_worker_fetches_footprint_and_step_for_model_only(monkeypatch):
    import lcsc2altium.ui.main_window as main_window

    client = MagicMock()
    client.__enter__.return_value = client
    client_kwargs = []

    def make_client(**kwargs):
        client_kwargs.append(kwargs)
        return client

    monkeypatch.setattr(main_window, "EasyedaClient", make_client)

    calls = []
    entry = ComponentEntry(
        info=ComponentInfo(lcsc="C12345"),
        symbol=SymbolData(name="SYM"),
        footprint=FootprintData(name="FP"),
        step_data=b"step",
    )

    def load(_client, lcsc, **kwargs):
        calls.append((lcsc, kwargs))
        return entry

    monkeypatch.setattr(main_window, "load_component", load)
    worker = DownloadWorker(
        ["C12345"],
        {CONTENT_MODELS},
        proxy_mode="manual",
        proxy_address="proxy.example",
        proxy_port=8080,
    )
    results = []
    worker.finished.connect(lambda entries, errors: results.append((entries, errors)))
    worker.run()

    assert calls == [("C12345", {"want_footprint": True, "want_step": True})]
    assert client_kwargs == [{
        "proxy_mode": "manual",
        "proxy_address": "proxy.example",
        "proxy_port": 8080,
    }]
    assert entry.include_model is True
    assert results == [([entry], [])]


def test_download_worker_stops_batch_after_easyeda_denies_access(monkeypatch):
    import lcsc2altium.ui.main_window as main_window

    client = MagicMock()
    client.__enter__.return_value = client
    monkeypatch.setattr(main_window, "EasyedaClient", lambda **kwargs: client)

    attempted_parts = []

    def deny_component(_client, lcsc, **kwargs):
        attempted_parts.append(lcsc)
        raise EasyedaAccessDeniedError("HTTP 403 for EasyEDA")

    monkeypatch.setattr(main_window, "load_component", deny_component)
    worker = DownloadWorker(["C123", "C456", "C789"], {CONTENT_SYMBOLS})
    results = []
    worker.finished.connect(lambda entries, errors: results.append((entries, errors)))

    worker.run()

    assert attempted_parts == ["C123"]
    assert results[0][0] == []
    assert "C123: HTTP 403" in results[0][1][0]
    assert "2 remaining component(s) were not requested" in results[0][1][1]


def test_download_worker_fetches_step_from_cached_footprint_without_component_api(monkeypatch):
    import lcsc2altium.ui.main_window as main_window

    client = MagicMock()
    client.__enter__.return_value = client
    client.download_step.return_value = b"new step"
    monkeypatch.setattr(main_window, "EasyedaClient", lambda **kwargs: client)

    cached = ComponentEntry(
        info=ComponentInfo(lcsc="C12345"),
        symbol=SymbolData(name="SYM"),
        footprint=FootprintData(
            name="FP",
            step=StepModelRef(uuid="model-uuid"),
        ),
        include_symbol=True,
        include_footprint=True,
    )
    worker = DownloadWorker(
        ["C12345"],
        {CONTENT_MODELS},
        existing_entries={"C12345": cached},
    )
    results = []
    worker.finished.connect(lambda entries, errors: results.append((entries, errors)))
    worker.run()

    client.fetch_component.assert_not_called()
    client.download_step.assert_called_once_with("model-uuid")
    assert results[0][0][0].step_data == b"new step"
    assert results[0][0][0].include_model is True
    assert results[0][1] == []


def test_connection_test_worker_uses_current_proxy_settings(monkeypatch):
    import lcsc2altium.ui.main_window as main_window

    client = MagicMock()
    client.__enter__.return_value = client
    client_kwargs = []

    def make_client(**kwargs):
        client_kwargs.append(kwargs)
        return client

    monkeypatch.setattr(main_window, "EasyedaClient", make_client)
    worker = ConnectionTestWorker(
        proxy_mode="manual",
        proxy_address="proxy.example",
        proxy_port=3128,
    )
    results = []
    worker.finished.connect(lambda succeeded, message: results.append((succeeded, message)))
    worker.run()

    assert client_kwargs == [{
        "timeout": 10,
        "retries": 0,
        "proxy_mode": "manual",
        "proxy_address": "proxy.example",
        "proxy_port": 3128,
    }]
    client.check_connection.assert_called_once_with()
    assert results == [(
        True,
        "Connection to the EasyEDA component API is working with the selected settings.",
    )]


def test_model_download_update_adds_step_and_preserves_it_when_unselected():
    info = ComponentInfo(lcsc="C12345")
    existing = ComponentEntry(
        info=info,
        symbol=SymbolData(name="SYM"),
        footprint=FootprintData(name="FP"),
    )
    model_update = ComponentEntry(
        info=info,
        symbol=SymbolData(name="SYM"),
        footprint=FootprintData(name="FP"),
        step_data=b"step payload",
        include_symbol=False,
        include_footprint=True,
        include_model=True,
    )

    merge_component_update(existing, model_update)
    assert existing.step_data == b"step payload"
    assert existing.include_model is True

    options_without_models = ComponentEntry(
        info=info,
        symbol=SymbolData(name="SYM"),
        footprint=FootprintData(name="FP"),
        include_symbol=True,
        include_footprint=True,
        include_model=False,
    )
    merge_component_update(existing, options_without_models)
    assert existing.step_data == b"step payload"
