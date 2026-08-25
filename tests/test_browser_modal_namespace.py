from types import SimpleNamespace

from work_researcher.browser import _MODAL_TAGGER_JS, BrowserSession


def test_modal_snapshot_clears_background_element_numbers() -> None:
    clear = "document.querySelectorAll('[data-wr-n]').forEach"
    dialogs = "const dialogs = Array.from"

    assert clear in _MODAL_TAGGER_JS
    assert _MODAL_TAGGER_JS.index(clear) < _MODAL_TAGGER_JS.index(dialogs)


def test_browser_session_starts_without_a_trace_path() -> None:
    session = BrowserSession(SimpleNamespace(browser={}))

    assert session._trace_path is None
