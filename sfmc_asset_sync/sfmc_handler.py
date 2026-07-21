from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

import requests

if TYPE_CHECKING:
    from config import AppConfig

logger = logging.getLogger(__name__)


class SfmcApiError(Exception):
    pass


class SfmcClient:
    # Refresh the token this many seconds before its actual expiry to avoid
    # using a token that expires mid-request.
    _TOKEN_EXPIRY_BUFFER_SECONDS = 60

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._session = requests.Session()
        self._cached_token: str | None = None
        self._token_expires_at: float = 0.0

    def _fetch_access_token(self) -> tuple[str, int]:
        auth_url = f"{self._config.sfmc_auth_base_url}/v2/token"
        payload: dict[str, Any] = {
            "grant_type": "client_credentials",
            "client_id": self._config.sfmc_client_id,
            "client_secret": self._config.sfmc_client_secret,
        }
        if self._config.sfmc_account_id:
            payload["account_id"] = self._config.sfmc_account_id

        response = self._session.post(auth_url, json=payload, timeout=30)
        if response.status_code >= 400:
            raise SfmcApiError(
                f"Token request failed: HTTP {response.status_code} - {response.text}"
            )

        data = response.json()
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise SfmcApiError("Token response missing access_token")
        expires_in = data.get("expires_in")
        if not isinstance(expires_in, int) or expires_in <= 0:
            expires_in = 1200  # SFMC default token lifetime is 20 minutes
        return token, expires_in

    def _get_access_token(self, force_refresh: bool = False) -> str:
        if (
            not force_refresh
            and self._cached_token is not None
            and time.monotonic() < self._token_expires_at
        ):
            return self._cached_token

        token, expires_in = self._fetch_access_token()
        self._cached_token = token
        self._token_expires_at = (
            time.monotonic() + expires_in - self._TOKEN_EXPIRY_BUFFER_SECONDS
        )
        logger.info("Fetched new SFMC access token (expires in %ss).", expires_in)
        return token

    def get_asset_html(self, asset_id: int) -> str:
        url = f"{self._config.sfmc_rest_base_url}/asset/v1/content/assets/{asset_id}"
        token = self._get_access_token()
        response = self._session.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        if response.status_code == 401:
            # Token may have been revoked/expired early; refresh once and retry.
            token = self._get_access_token(force_refresh=True)
            response = self._session.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
        if response.status_code >= 400:
            raise SfmcApiError(
                f"Asset request failed for {asset_id}: HTTP {response.status_code} - {response.text}"
            )

        data = response.json()
        html = self._extract_html(data)
        if not html:
            raise SfmcApiError(f"No HTML content found for asset {asset_id}")
        return html

    @staticmethod
    def _extract_html(asset_payload: dict[str, Any]) -> str:
        views = asset_payload.get("views")
        if isinstance(views, dict):
            html_view = views.get("html")
            if isinstance(html_view, dict):
                html_content = html_view.get("content")
                if isinstance(html_content, str):
                    return html_content

        content = asset_payload.get("content")
        if isinstance(content, str):
            return content

        return ""
