import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import fetch_data  # noqa: E402


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.ok, self.text = data, status, status < 400, ""

    def json(self):
        return self._data

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(self.status_code)


class _Session:
    def __init__(self, pages):
        self.pages, self.calls = list(pages), []

    def post(self, url, json, timeout):
        self.calls.append((url, dict(json)))
        return _Resp(self.pages.pop(0))


def test_search_follows_next_page_token(monkeypatch):
    monkeypatch.setattr(fetch_data, "parse_issue", lambda i: i["key"])
    s = _Session([
        {"issues": [{"key": "A"}, {"key": "B"}], "nextPageToken": "t2"},
        {"issues": [{"key": "C"}]},
    ])
    assert fetch_data.search(s, "project = AFH") == ["A", "B", "C"]
    assert all(url.endswith("/search/jql") for url, _ in s.calls)
    assert "nextPageToken" not in s.calls[0][1]
    assert s.calls[1][1]["nextPageToken"] == "t2"
