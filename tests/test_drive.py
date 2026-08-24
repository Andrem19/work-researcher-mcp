from unittest.mock import MagicMock, patch

from work_researcher.config import Settings
from work_researcher.drive import build_service


def test_expired_oauth_credentials_refresh_with_transport_request(tmp_path):
    token = tmp_path / "secrets" / "google_token.json"
    token.parent.mkdir()
    token.write_text("{}", encoding="utf-8")
    settings = Settings(
        project_root=tmp_path,
        drive={"mode": "oauth", "token_file": "secrets/google_token.json"},
    )
    credentials = MagicMock(expired=True, refresh_token="refresh-token")
    request = object()
    service = object()

    with (
        patch(
            "google.oauth2.credentials.Credentials.from_authorized_user_file",
            return_value=credentials,
        ),
        patch("google.auth.transport.requests.Request", return_value=request),
        patch("googleapiclient.discovery.build", return_value=service),
    ):
        result = build_service(settings)

    credentials.refresh.assert_called_once_with(request)
    assert result is service
