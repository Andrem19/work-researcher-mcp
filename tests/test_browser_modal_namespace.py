from types import SimpleNamespace
from unittest.mock import patch

from work_researcher.browser import _MODAL_TAGGER_JS, BrowserSession


def test_modal_snapshot_clears_background_element_numbers() -> None:
    clear = "document.querySelectorAll('[data-wr-n]').forEach"
    dialogs = "const dialogs = Array.from"

    assert clear in _MODAL_TAGGER_JS
    assert _MODAL_TAGGER_JS.index(clear) < _MODAL_TAGGER_JS.index(dialogs)


def test_browser_session_starts_without_a_trace_path() -> None:
    session = BrowserSession(SimpleNamespace(browser={}))

    assert session._trace_path is None


def test_stale_process_cleanup_targets_only_the_active_candidate_profile() -> None:
    settings = SimpleNamespace(
        browser={}, browser_profile_dir=r"D:\profiles\partner\data\browser_profile"
    )
    session = BrowserSession(settings)

    with patch("subprocess.run") as run, patch("time.sleep"):
        session._kill_profile_processes()

    command = run.call_args.args[0][-1]
    process_env = run.call_args.kwargs["env"]
    assert process_env["WORK_RESEARCHER_BROWSER_PROFILE"] == settings.browser_profile_dir
    assert "$env:WORK_RESEARCHER_BROWSER_PROFILE" in command
    assert "'*browser_profile*'" not in command
