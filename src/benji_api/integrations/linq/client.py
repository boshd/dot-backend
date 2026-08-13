from typing import Any
from urllib.parse import quote

import httpx


class LinqClientError(RuntimeError):
    pass


class LinqClient:
    def __init__(self, *, api_key: str, base_url: str, timeout_seconds: float = 8) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    async def send_chat_message(
        self,
        *,
        chat_id: str,
        text: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = {
            "message": {
                "parts": [{"type": "text", "value": text}],
                "idempotency_key": idempotency_key,
            }
        }
        return await self._request("POST", f"/chats/{chat_id}/messages", json=payload)

    async def get_chat(self, *, chat_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/chats/{chat_id}")

    async def start_typing(self, *, chat_id: str) -> None:
        await self._request("POST", f"/chats/{chat_id}/typing")

    async def stop_typing(self, *, chat_id: str) -> None:
        await self._request("DELETE", f"/chats/{chat_id}/typing")

    async def mark_chat_read(self, *, chat_id: str) -> None:
        await self._request("POST", f"/chats/{chat_id}/read")

    async def share_contact_card(self, *, chat_id: str) -> None:
        await self._request("POST", f"/chats/{chat_id}/share_contact_card")

    async def add_message_reaction(
        self,
        *,
        message_id: str,
        reaction_type: str,
        part_index: int = 0,
    ) -> dict[str, Any]:
        safe_id = quote(message_id, safe="")
        return await self._request(
            "POST",
            f"/messages/{safe_id}/reactions",
            json={
                "operation": "add",
                "type": reaction_type,
                "part_index": part_index,
            },
        )

    async def delete_attachment(self, *, attachment_id: str) -> None:
        safe_id = quote(attachment_id, safe="")
        await self._request("DELETE", f"/attachments/{safe_id}")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.request(
                method,
                f"{self._base_url}{path}",
                headers=headers,
                json=json,
            )

        if response.is_error:
            trace_id = response.headers.get("X-Trace-ID", "unknown")
            detail = response.text[:500]
            raise LinqClientError(
                f"Linq returned {response.status_code} (trace_id={trace_id}): {detail}"
            )

        if response.status_code == 204 or not response.content:
            return {}
        return response.json()
