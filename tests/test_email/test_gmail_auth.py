"""Tests for gmail/auth.py token refresh fallback behavior."""

from unittest.mock import MagicMock, patch

from google.auth.exceptions import RefreshError

from jobhunter.gmail.auth import get_gmail_service


class TestGetGmailService:
    @patch("jobhunter.gmail.auth._save_token")
    @patch("jobhunter.gmail.auth._run_oauth_flow")
    @patch("jobhunter.gmail.auth._load_token")
    @patch("jobhunter.gmail.auth.build")
    def test_refresh_error_falls_back_to_oauth(
        self,
        mock_build,
        mock_load_token,
        mock_run_oauth,
        mock_save_token,
    ):
        expired_creds = MagicMock()
        expired_creds.valid = False
        expired_creds.expired = True
        expired_creds.refresh_token = "rtok"
        expired_creds.refresh.side_effect = RefreshError("invalid_grant")
        mock_load_token.return_value = expired_creds

        oauth_creds = MagicMock()
        oauth_creds.valid = True
        mock_run_oauth.return_value = oauth_creds

        get_gmail_service("creds.json", "token.json")

        mock_run_oauth.assert_called_once_with("creds.json")
        mock_save_token.assert_called_once_with(oauth_creds, "token.json")
        mock_build.assert_called_once_with("gmail", "v1", credentials=oauth_creds)

    @patch("jobhunter.gmail.auth._save_token")
    @patch("jobhunter.gmail.auth._run_oauth_flow")
    @patch("jobhunter.gmail.auth._load_token")
    @patch("jobhunter.gmail.auth.build")
    def test_valid_token_builds_service_without_oauth(
        self,
        mock_build,
        mock_load_token,
        mock_run_oauth,
        mock_save_token,
    ):
        valid_creds = MagicMock()
        valid_creds.valid = True
        mock_load_token.return_value = valid_creds

        get_gmail_service("creds.json", "token.json")

        mock_run_oauth.assert_not_called()
        mock_save_token.assert_not_called()
        mock_build.assert_called_once_with("gmail", "v1", credentials=valid_creds)
