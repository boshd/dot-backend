import asyncio
import base64
import re
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from benji_api.integrations.types import (
    CalendarEvent,
    CalendarEventPage,
    GmailMessage,
    GmailMessagePage,
    GmailMessageSummary,
    OAuthTokenSet,
    ProviderAccountProfile,
    ProviderSubscription,
)

GOOGLE_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_TOKEN_REVOCATION_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_CALENDAR_API = "https://www.googleapis.com/calendar/v3"
GOOGLE_GMAIL_API = "https://gmail.googleapis.com/gmail/v1"


class GoogleProviderError(RuntimeError):
    pass


class GoogleIntegrationClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        timeout_seconds: float = 10,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.timeout_seconds = timeout_seconds

    def authorization_url(self, *, state: str, scopes: tuple[str, ...]) -> str:
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": " ".join(scopes),
                "access_type": "offline",
                "include_granted_scopes": "true",
                "prompt": "consent select_account",
                "state": state,
            }
        )
        return f"{GOOGLE_AUTHORIZATION_URL}?{query}"

    async def exchange_code(self, code: str) -> OAuthTokenSet:
        response = await _request(
            "POST",
            GOOGLE_TOKEN_URL,
            timeout=self.timeout_seconds,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": self.redirect_uri,
            },
        )
        payload = _json_response(response, "Google OAuth token exchange failed")
        return _token_set(payload)

    async def refresh_access_token(self, refresh_token: str) -> OAuthTokenSet:
        response = await _request(
            "POST",
            GOOGLE_TOKEN_URL,
            timeout=self.timeout_seconds,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        payload = _json_response(response, "Google OAuth token refresh failed")
        return _token_set(payload)

    async def get_account_profile(self, access_token: str) -> ProviderAccountProfile:
        response = await _request(
            "GET",
            GOOGLE_USERINFO_URL,
            timeout=self.timeout_seconds,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        payload = _json_response(response, "Google account lookup failed")
        account_id = payload.get("sub")
        email = payload.get("email")
        if not isinstance(account_id, str) or not isinstance(email, str):
            raise GoogleProviderError("Google returned an incomplete account profile")
        return ProviderAccountProfile(
            account_id=account_id,
            email=email.lower(),
            display_name=payload.get("name") if isinstance(payload.get("name"), str) else None,
            avatar_url=(
                payload.get("picture") if isinstance(payload.get("picture"), str) else None
            ),
            email_verified=payload.get("email_verified") is True,
        )

    async def watch_calendar(
        self,
        *,
        access_token: str,
        channel_id: str,
        webhook_url: str,
        verification_token: str,
    ) -> ProviderSubscription:
        requested_expiration = datetime.now(UTC) + timedelta(days=7)
        response = await _request(
            "POST",
            f"{GOOGLE_CALENDAR_API}/calendars/primary/events/watch",
            timeout=self.timeout_seconds,
            headers={"Authorization": f"Bearer {access_token}"},
            json={
                "id": channel_id,
                "type": "web_hook",
                "address": webhook_url,
                "token": verification_token,
                "expiration": str(int(requested_expiration.timestamp() * 1000)),
            },
        )
        payload = _json_response(response, "Google Calendar watch setup failed")
        return ProviderSubscription(
            subscription_id=str(payload.get("id", channel_id)),
            resource_id=_optional_string(payload.get("resourceId")),
            cursor=None,
            expires_at=_milliseconds_datetime(payload.get("expiration")),
        )

    async def watch_gmail(
        self,
        *,
        access_token: str,
        topic_name: str,
        subscription_id: str,
    ) -> ProviderSubscription:
        response = await _request(
            "POST",
            f"{GOOGLE_GMAIL_API}/users/me/watch",
            timeout=self.timeout_seconds,
            headers={"Authorization": f"Bearer {access_token}"},
            json={"topicName": topic_name},
        )
        payload = _json_response(response, "Gmail watch setup failed")
        return ProviderSubscription(
            subscription_id=subscription_id,
            resource_id=None,
            cursor=_optional_string(payload.get("historyId")),
            expires_at=_milliseconds_datetime(payload.get("expiration")),
        )

    async def stop_calendar_watch(
        self,
        *,
        access_token: str,
        channel_id: str,
        resource_id: str,
    ) -> None:
        response = await _request(
            "POST",
            f"{GOOGLE_CALENDAR_API}/channels/stop",
            timeout=self.timeout_seconds,
            headers={"Authorization": f"Bearer {access_token}"},
            json={"id": channel_id, "resourceId": resource_id},
        )
        _ensure_success(
            response,
            "Google Calendar notification shutdown failed",
            already_gone_statuses={404, 410},
        )

    async def stop_gmail_watch(self, *, access_token: str) -> None:
        response = await _request(
            "POST",
            f"{GOOGLE_GMAIL_API}/users/me/stop",
            timeout=self.timeout_seconds,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        _ensure_success(
            response,
            "Gmail notification shutdown failed",
            already_gone_statuses={404, 410},
        )

    async def revoke_token(self, token: str) -> None:
        response = await _request(
            "POST",
            GOOGLE_TOKEN_REVOCATION_URL,
            timeout=self.timeout_seconds,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"token": token},
        )
        # Google's invalid-token response means the credential is already unusable.
        _ensure_success(
            response,
            "Google access revocation failed",
            already_gone_statuses={400},
        )

    async def list_calendar_events(
        self,
        *,
        access_token: str,
        time_min: datetime,
        time_max: datetime,
        time_zone: str,
        max_results: int = 100,
    ) -> CalendarEventPage:
        response = await _request(
            "GET",
            f"{GOOGLE_CALENDAR_API}/calendars/primary/events",
            timeout=self.timeout_seconds,
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "timeMin": time_min.isoformat(),
                "timeMax": time_max.isoformat(),
                "timeZone": time_zone,
                "singleEvents": "true",
                "orderBy": "startTime",
                "showDeleted": "false",
                "maxResults": str(max(1, min(max_results, 250))),
            },
        )
        payload = _json_response(response, "Google Calendar event lookup failed")
        raw_events = payload.get("items", [])
        if not isinstance(raw_events, list):
            raise GoogleProviderError("Google returned invalid Calendar events")
        events = tuple(
            event
            for item in raw_events
            if isinstance(item, dict) and (event := _calendar_event(item)) is not None
        )
        return CalendarEventPage(
            events=events,
            truncated=isinstance(payload.get("nextPageToken"), str),
            calendar_timezone=_optional_string(payload.get("timeZone")),
        )

    async def search_gmail_messages(
        self,
        *,
        access_token: str,
        query: str,
        max_results: int = 10,
    ) -> GmailMessagePage:
        result_limit = max(1, min(max_results, 10))
        response = await _request(
            "GET",
            f"{GOOGLE_GMAIL_API}/users/me/messages",
            timeout=self.timeout_seconds,
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "q": query,
                "maxResults": str(result_limit),
                "includeSpamTrash": "false",
            },
        )
        payload = _json_response(response, "Gmail search failed")
        raw_messages = payload.get("messages", [])
        if not isinstance(raw_messages, list):
            raise GoogleProviderError("Google returned invalid Gmail search results")
        message_ids = [
            item["id"]
            for item in raw_messages[:result_limit]
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        detail_payloads = await asyncio.gather(
            *(
                self._get_gmail_payload(
                    access_token=access_token,
                    message_id=message_id,
                    format_name="metadata",
                )
                for message_id in message_ids
            )
        )
        return GmailMessagePage(
            messages=tuple(_gmail_summary(item) for item in detail_payloads),
            truncated=isinstance(payload.get("nextPageToken"), str),
        )

    async def get_gmail_message(
        self,
        *,
        access_token: str,
        message_id: str,
        max_body_chars: int = 12_000,
    ) -> GmailMessage:
        payload = await self._get_gmail_payload(
            access_token=access_token,
            message_id=message_id,
            format_name="full",
        )
        return _gmail_message(payload, max_body_chars=max_body_chars)

    async def _get_gmail_payload(
        self,
        *,
        access_token: str,
        message_id: str,
        format_name: str,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"format": format_name}
        if format_name == "metadata":
            params["metadataHeaders"] = ["From", "To", "Subject", "Date"]
        response = await _request(
            "GET",
            f"{GOOGLE_GMAIL_API}/users/me/messages/{quote(message_id, safe='')}",
            timeout=self.timeout_seconds,
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
        )
        return _json_response(response, "Gmail message lookup failed")


def _json_response(response: httpx.Response, fallback: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as error:
        raise GoogleProviderError(fallback) from error
    if response.is_error:
        detail = payload.get("error_description")
        if not isinstance(detail, str):
            nested = payload.get("error")
            detail = nested.get("message") if isinstance(nested, dict) else nested
        raise GoogleProviderError(detail if isinstance(detail, str) else fallback)
    if not isinstance(payload, dict):
        raise GoogleProviderError(fallback)
    return payload


def _ensure_success(
    response: httpx.Response,
    fallback: str,
    *,
    already_gone_statuses: set[int] | None = None,
) -> None:
    if response.status_code in (already_gone_statuses or set()):
        return
    if not response.is_error:
        return
    try:
        payload = response.json()
    except ValueError as error:
        raise GoogleProviderError(fallback) from error
    detail = payload.get("error_description") if isinstance(payload, dict) else None
    if not isinstance(detail, str) and isinstance(payload, dict):
        nested = payload.get("error")
        detail = nested.get("message") if isinstance(nested, dict) else nested
    raise GoogleProviderError(detail if isinstance(detail, str) else fallback)


async def _request(
    method: str,
    url: str,
    *,
    timeout: float,
    **kwargs: Any,
) -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.request(method, url, **kwargs)
    except httpx.HTTPError as error:
        raise GoogleProviderError("Google is temporarily unavailable") from error


def _token_set(payload: dict[str, Any]) -> OAuthTokenSet:
    access_token = payload.get("access_token")
    if not isinstance(access_token, str):
        raise GoogleProviderError("Google did not return an access token")
    expires_in = payload.get("expires_in")
    expires_at = (
        datetime.now(UTC) + timedelta(seconds=int(expires_in))
        if isinstance(expires_in, (int, float, str))
        else None
    )
    scope_value = payload.get("scope", "")
    scopes = tuple(scope_value.split()) if isinstance(scope_value, str) else ()
    refresh_token = payload.get("refresh_token")
    return OAuthTokenSet(
        access_token=access_token,
        refresh_token=refresh_token if isinstance(refresh_token, str) else None,
        scopes=scopes,
        expires_at=expires_at,
    )


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _milliseconds_datetime(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC) if value is not None else None
    except (TypeError, ValueError, OSError):
        return None


def _calendar_event(payload: dict[str, Any]) -> CalendarEvent | None:
    event_id = payload.get("id")
    start_payload = payload.get("start")
    end_payload = payload.get("end")
    if (
        not isinstance(event_id, str)
        or not isinstance(start_payload, dict)
        or not isinstance(end_payload, dict)
    ):
        return None
    start = start_payload.get("dateTime") or start_payload.get("date")
    end = end_payload.get("dateTime") or end_payload.get("date")
    if not isinstance(start, str) or not isinstance(end, str):
        return None
    organizer = payload.get("organizer")
    attendees = payload.get("attendees")
    return CalendarEvent(
        event_id=event_id,
        title=(
            payload.get("summary")
            if isinstance(payload.get("summary"), str)
            else "(untitled event)"
        ),
        start=start,
        end=end,
        all_day="date" in start_payload,
        status=(payload.get("status") if isinstance(payload.get("status"), str) else "confirmed"),
        location=(payload.get("location") if isinstance(payload.get("location"), str) else None),
        organizer_email=(
            organizer.get("email")
            if isinstance(organizer, dict) and isinstance(organizer.get("email"), str)
            else None
        ),
        attendee_count=len(attendees) if isinstance(attendees, list) else 0,
        html_link=(payload.get("htmlLink") if isinstance(payload.get("htmlLink"), str) else None),
    )


def _gmail_summary(payload: dict[str, Any]) -> GmailMessageSummary:
    message_id = payload.get("id")
    thread_id = payload.get("threadId")
    if not isinstance(message_id, str) or not isinstance(thread_id, str):
        raise GoogleProviderError("Google returned an invalid Gmail message")
    headers = _gmail_headers(payload)
    return GmailMessageSummary(
        message_id=message_id,
        thread_id=thread_id,
        subject=headers.get("subject", "(no subject)"),
        sender=headers.get("from"),
        recipients=_split_header_addresses(headers.get("to")),
        sent_at=headers.get("date"),
        snippet=_optional_string(payload.get("snippet")) or "",
        labels=_string_tuple(payload.get("labelIds")),
    )


def _gmail_message(payload: dict[str, Any], *, max_body_chars: int) -> GmailMessage:
    summary = _gmail_summary(payload)
    message_payload = payload.get("payload")
    plain_parts: list[str] = []
    html_parts: list[str] = []
    if isinstance(message_payload, dict):
        _collect_gmail_body(message_payload, plain_parts=plain_parts, html_parts=html_parts)
    body = "\n\n".join(part for part in plain_parts if part.strip()).strip()
    if not body and html_parts:
        parser = _TextHTMLParser()
        parser.feed("\n\n".join(html_parts))
        body = parser.text
    body = re.sub(r"[ \t]+", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    limit = max(1, min(max_body_chars, 20_000))
    truncated = len(body) > limit
    if truncated:
        body = body[:limit].rstrip() + "…"
    headers = _gmail_headers(payload)
    return GmailMessage(
        message_id=summary.message_id,
        thread_id=summary.thread_id,
        subject=summary.subject,
        sender=summary.sender,
        recipients=summary.recipients,
        cc=_split_header_addresses(headers.get("cc")),
        sent_at=summary.sent_at,
        snippet=summary.snippet,
        labels=summary.labels,
        body_text=body,
        body_truncated=truncated,
    )


def _gmail_headers(payload: dict[str, Any]) -> dict[str, str]:
    message_payload = payload.get("payload")
    raw_headers = message_payload.get("headers", []) if isinstance(message_payload, dict) else []
    if not isinstance(raw_headers, list):
        return {}
    headers: dict[str, str] = {}
    for item in raw_headers:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if isinstance(name, str) and isinstance(value, str):
            headers[name.casefold()] = value
    return headers


def _collect_gmail_body(
    payload: dict[str, Any], *, plain_parts: list[str], html_parts: list[str]
) -> None:
    mime_type = payload.get("mimeType")
    body = payload.get("body")
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, str):
        decoded = _decode_base64url(data)
        if mime_type == "text/plain":
            plain_parts.append(decoded)
        elif mime_type == "text/html":
            html_parts.append(decoded)
    parts = payload.get("parts", [])
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict):
                _collect_gmail_body(part, plain_parts=plain_parts, html_parts=html_parts)


def _decode_base64url(value: str) -> str:
    try:
        padded = value + "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def _split_header_addresses(value: str | None) -> tuple[str, ...]:
    return tuple(part.strip() for part in (value or "").split(",") if part.strip())


def _string_tuple(value: Any) -> tuple[str, ...]:
    return tuple(item for item in value if isinstance(item, str)) if isinstance(value, list) else ()


class _TextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style"}:
            self._ignored_depth += 1
        elif tag in {"br", "div", "p", "li", "tr"} and not self._ignored_depth:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif tag in {"div", "p", "li", "tr"} and not self._ignored_depth:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self._parts.append(data)

    @property
    def text(self) -> str:
        return "".join(self._parts)
