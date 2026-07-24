"""Fetch layer: cookie loading and status handling. The live network path
(Cloudflare + curl_cffi Chrome impersonation) is validated by hand, not in CI;
here we pin the offline logic — cookie resolution and the loud-on-block contract
— with a fake session so a regression can't quietly swallow a 403."""

import pytest

from core.config import settings
from ratings_ingest import fetch
from ratings_ingest.fetch import SourceError


class _Resp:
    def __init__(self, status=200, text="<html><tbody></tbody></html>",
                 ctype="text/html; charset=UTF-8"):
        self.status_code = status
        self.text = text
        self.headers = {"content-type": ctype}


class _Sess:
    def __init__(self, resp):
        self._resp = resp
        self.calls: list = []

    def get(self, url, params=None, allow_redirects=None):
        self.calls.append((url, params, allow_redirects))
        return self._resp


def test_load_cookie_from_file(tmp_path, monkeypatch):
    f = tmp_path / "cookie.txt"
    f.write_text("cf_clearance=abc; r=260045\n", encoding="utf-8")
    monkeypatch.setattr(settings(), "sofifa_cookie", "")
    monkeypatch.setattr(settings(), "sofifa_cookie_file", f)
    assert fetch.load_cookie() == "cf_clearance=abc; r=260045"


def test_load_cookie_strips_devtools_prefix(tmp_path, monkeypatch):
    f = tmp_path / "cookie.txt"
    f.write_text("Cookie: cf_clearance=abc", encoding="utf-8")
    monkeypatch.setattr(settings(), "sofifa_cookie", "")
    monkeypatch.setattr(settings(), "sofifa_cookie_file", f)
    assert fetch.load_cookie() == "cf_clearance=abc"


def test_env_cookie_wins_over_file(tmp_path, monkeypatch):
    f = tmp_path / "cookie.txt"
    f.write_text("cf_clearance=fromfile", encoding="utf-8")
    monkeypatch.setattr(settings(), "sofifa_cookie", "cf_clearance=fromenv")
    monkeypatch.setattr(settings(), "sofifa_cookie_file", f)
    assert fetch.load_cookie() == "cf_clearance=fromenv"


def test_missing_cookie_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(settings(), "sofifa_cookie", "")
    monkeypatch.setattr(settings(), "sofifa_cookie_file", tmp_path / "nope.txt")
    with pytest.raises(SourceError):
        fetch.load_cookie()


def test_fetch_page_returns_html_and_sends_sort_params():
    sess = _Sess(_Resp(200, text="<tbody>ok</tbody>"))
    out = fetch.fetch_page(sess, offset=120)
    assert out == "<tbody>ok</tbody>"
    _url, params, redirects = sess.calls[0]
    assert params == {"offset": 120, "col": "oa", "sort": "desc", "r": "250044"}
    assert redirects is False  # a redirect to a challenge is a block, not a hop


def test_fetch_page_default_sort_omitted_when_col_blank():
    sess = _Sess(_Resp(200))
    fetch.fetch_page(sess, offset=0, col="")
    assert sess.calls[0][1] == {"offset": 0, "r": "250044"}


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_blocked_status_raises_loudly(status):
    sess = _Sess(_Resp(status))
    with pytest.raises(SourceError, match="re-paste"):
        fetch.fetch_page(sess, offset=0)


def test_non_html_response_raises():
    sess = _Sess(_Resp(200, ctype="application/json"))
    with pytest.raises(SourceError, match="challenge"):
        fetch.fetch_page(sess, offset=0)
