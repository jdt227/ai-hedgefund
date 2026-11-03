from datetime import date

import pytest
import requests

from src import http_utils
from src.http_utils import cache_path, fetch

RUN = date(2030, 1, 15)
URL = "https://example.com/data"


class FakeResponse:
    def __init__(self, status_code=200, data=None, content=b""):
        self.status_code = status_code
        self._data = data
        self.content = content

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data


@pytest.fixture
def get(monkeypatch):
    """Queue responses for requests.get and record calls."""
    calls = []
    queue = []

    def fake(url, params=None, headers=None, timeout=None):
        calls.append(params)
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(http_utils.requests, "get", fake)
    monkeypatch.setattr(http_utils.time, "sleep", lambda s: None)
    return queue, calls


def test_second_call_same_day_uses_cache(get, tmp_path):
    queue, calls = get
    queue.append(FakeResponse(200, {"a": 1}))
    first = fetch("p", URL, run_date=RUN, cache_root=tmp_path)
    second = fetch("p", URL, run_date=RUN, cache_root=tmp_path)
    assert first.status == "ok" and second.status == "cached"
    assert second.data == {"a": 1} and second.retrieved_at == first.retrieved_at and len(calls) == 1


def test_cache_layout_is_provider_and_date(tmp_path):
    path = cache_path("fred", URL, {"series_id": "X"}, RUN, tmp_path)
    assert path.parent == tmp_path / "fred" / "2030-01-15"


def test_secret_is_not_in_cache_key_or_file(get, tmp_path):
    queue, _ = get
    queue.append(FakeResponse(200, {"a": 1}))
    fetch("p", URL, params={"token": "SECRET123"}, run_date=RUN, cache_root=tmp_path)
    assert cache_path("p", URL, {"token": "other"}, RUN, tmp_path).exists()
    assert not list(tmp_path.rglob("*SECRET123*"))
    assert all("SECRET123" not in f.read_text() for f in tmp_path.rglob("*.json"))


def test_retries_then_succeeds(get, tmp_path):
    queue, calls = get
    queue += [FakeResponse(503), requests.ConnectionError("down"), FakeResponse(200, {"ok": True})]
    assert fetch("p", URL, run_date=RUN, cache_root=tmp_path).status == "ok"
    assert len(calls) == 3


def test_outage_returns_status_without_raising_or_leaking_key(get, tmp_path):
    queue, _ = get
    queue += [requests.ConnectionError("failed for url?token=SECRET123")] * 3
    result = fetch("p", URL, params={"token": "SECRET123"}, run_date=RUN, cache_root=tmp_path)
    assert result.status == "error" and "SECRET123" not in result.message
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize("code,status", [(403, "forbidden"), (401, "forbidden"), (404, "not_found"), (400, "error")])
def test_client_errors_are_not_retried(get, tmp_path, code, status):
    queue, calls = get
    queue.append(FakeResponse(code))
    assert fetch("p", URL, run_date=RUN, cache_root=tmp_path).status == status
    assert len(calls) == 1


def test_rate_limit_status_after_retries(get, tmp_path):
    queue, _ = get
    queue += [FakeResponse(429)] * 3
    assert fetch("p", URL, run_date=RUN, cache_root=tmp_path).status == "rate_limited"


def test_text_mode_strips_bom(get, tmp_path):
    queue, _ = get
    queue.append(FakeResponse(200, content=b"\xef\xbb\xbf<rss/>"))
    assert fetch("p", URL, run_date=RUN, as_text=True, cache_root=tmp_path).data == "<rss/>"
