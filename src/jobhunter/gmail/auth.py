"""Gmail OAuth2 flow and token management."""

import os
import sys
from pathlib import Path
from typing import Optional

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow, WSGITimeoutError
from googleapiclient.discovery import build

from ..utils.logging import get_logger

logger = get_logger(__name__)

# Scopes required by the email agent
_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]

_DEFAULT_CREDENTIALS_PATH = "data/gmail_credentials.json"
_DEFAULT_TOKEN_PATH = "data/gmail_token.json"


def get_gmail_service(
    credentials_path: Optional[str] = None,
    token_path: Optional[str] = None,
):
    """
    Build and return an authenticated Gmail API service object.

    On first run, opens a browser window for the user to authorise access.
    The resulting token is saved to token_path and reused (with automatic
    refresh) on subsequent runs.

    Args:
        credentials_path: Path to the OAuth2 client credentials JSON downloaded
                          from Google Cloud Console. Falls back to
                          GMAIL_CREDENTIALS_PATH env var or the default path.
        token_path: Where to store the access/refresh token. Falls back to
                    GMAIL_TOKEN_PATH env var or the default path.

    Returns:
        Authenticated googleapiclient Resource for the Gmail v1 API.
    """
    credentials_path = (
        credentials_path
        or os.environ.get("GMAIL_CREDENTIALS_PATH", _DEFAULT_CREDENTIALS_PATH)
    )
    token_path = (
        token_path
        or os.environ.get("GMAIL_TOKEN_PATH", _DEFAULT_TOKEN_PATH)
    )

    creds = _load_token(token_path)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            logger.info("Refreshing expired Gmail token")
            try:
                creds.refresh(Request())
            except RefreshError as e:
                # Refresh token was revoked/expired (invalid_grant): fall back to
                # interactive OAuth so scheduler startup can recover automatically.
                logger.warning(
                    f"Gmail token refresh failed ({e}); starting OAuth flow"
                )
                creds = _run_oauth_flow(credentials_path)
        else:
            creds = _run_oauth_flow(credentials_path)

        _save_token(creds, token_path)

    return build("gmail", "v1", credentials=creds)


def _load_token(token_path: str) -> Optional[Credentials]:
    path = Path(token_path)
    if path.exists():
        try:
            return Credentials.from_authorized_user_file(str(path), _SCOPES)
        except Exception as e:
            logger.warning(f"Could not load Gmail token from {token_path}: {e}")
    return None


def _save_token(creds: Credentials, token_path: str) -> None:
    path = Path(token_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json())
    path.chmod(0o600)
    logger.info(f"Gmail token saved to {token_path}")


def _run_oauth_flow(credentials_path: str) -> Credentials:
    creds_path = Path(credentials_path)
    if not creds_path.exists():
        raise FileNotFoundError(
            f"Gmail credentials not found at {credentials_path}.\n"
            "Download OAuth2 credentials from Google Cloud Console:\n"
            "  APIs & Services → Credentials → Create OAuth Client ID → Desktop App\n"
            f"Save the JSON to {credentials_path}"
        )

    logger.info("Starting Gmail OAuth2 flow — browser will open for authorisation")
    print(
        "\n[JobHunter] Gmail authorisation required.\n"
        "  A browser window will open — sign in with your job-search Gmail account\n"
        "  and grant the requested permissions.\n"
    )
    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), _SCOPES)
    try:
        creds = flow.run_local_server(port=0, open_browser=True, timeout_seconds=300)
    except WSGITimeoutError:
        if not sys.stdin.isatty():
            raise

        print(
            "\n[JobHunter] Local OAuth callback was not captured.\n"
            "  Copy the full final localhost URL from the browser address bar\n"
            "  and paste it below.\n"
        )
        authorization_response = input("Redirect URL: ").strip()
        if not authorization_response:
            raise
        if authorization_response.startswith("http://"):
            authorization_response = authorization_response.replace(
                "http://", "https://", 1
            )
        flow.fetch_token(authorization_response=authorization_response)
        creds = flow.credentials

    logger.info("Gmail OAuth2 flow completed")
    return creds
