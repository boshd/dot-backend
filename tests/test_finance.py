import hashlib
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from benji_api.agents.tools import (
    CreateFinancialGoalTool,
    FinancialOverviewTool,
    SearchFinancialTransactionsTool,
)
from benji_api.agents.types import ToolContext
from benji_api.config import Settings
from benji_api.db.base import Base
from benji_api.integrations.plaid.client import PlaidClient
from benji_api.models import (
    Conversation,
    FinancialAccount,
    FinancialConnection,
    FinancialGoal,
    FinancialTransaction,
    ScheduledTask,
    User,
    UserEvent,
)
from benji_api.services.finance import (
    complete_plaid_link,
    create_plaid_link_token,
    disconnect_financial_connection,
    sync_financial_connection,
)


class FakePlaidClient:
    def __init__(self) -> None:
        self.link_requests: list[dict[str, object]] = []
        self.exchange_calls = 0
        self.removed_items: list[str] = []

    async def create_link_token(self, **arguments: object) -> dict[str, object]:
        self.link_requests.append(arguments)
        return {
            "link_token": "link-sandbox-test",
            "expiration": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
        }

    async def exchange_public_token(self, public_token: str) -> dict[str, str]:
        self.exchange_calls += 1
        assert public_token == "public-sandbox-test"
        return {"access_token": "access-sandbox-test", "item_id": "item-one"}

    async def sync_transactions(
        self, *, access_token: str, cursor: str | None
    ) -> dict[str, object]:
        assert access_token == "access-sandbox-test"
        assert cursor is None
        return {
            "accounts": [
                {
                    "account_id": "account-one",
                    "name": "Checking",
                    "official_name": "Everyday Checking",
                    "mask": "1234",
                    "type": "depository",
                    "subtype": "checking",
                    "balances": {
                        "current": 2400.5,
                        "available": 2300.5,
                        "iso_currency_code": "USD",
                    },
                }
            ],
            "added": [
                {
                    "transaction_id": "transaction-coffee",
                    "account_id": "account-one",
                    "amount": 8.75,
                    "iso_currency_code": "USD",
                    "date": datetime.now(UTC).date().isoformat(),
                    "authorized_date": datetime.now(UTC).date().isoformat(),
                    "name": "Coffee shop",
                    "merchant_name": "Daybreak Coffee",
                    "pending": False,
                    "personal_finance_category": {
                        "primary": "FOOD_AND_DRINK",
                        "detailed": "FOOD_AND_DRINK_COFFEE",
                    },
                },
                {
                    "transaction_id": "transaction-payroll",
                    "account_id": "account-one",
                    "amount": -1500,
                    "iso_currency_code": "USD",
                    "date": datetime.now(UTC).date().isoformat(),
                    "name": "Payroll",
                    "merchant_name": None,
                    "pending": False,
                },
            ],
            "modified": [],
            "removed": [],
            "next_cursor": "cursor-one",
            "has_more": False,
        }

    async def remove_item(self, access_token: str) -> None:
        self.removed_items.append(access_token)


class PlaidClientWithVerificationKey(PlaidClient):
    def __init__(self, key: dict[str, object]) -> None:
        super().__init__(client_id="client", secret="secret", base_url="https://example.com")
        self._key = key

    async def _post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        assert path == "/webhook_verification_key/get"
        assert payload["key_id"] == "key-one"
        return {"key": self._key}


@pytest.mark.anyio
async def test_plaid_webhook_signature_and_body_are_verified() -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    key = jwt.algorithms.ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    key.update({"kid": "key-one", "alg": "ES256", "use": "sig"})
    body = b'{"webhook_type":"TRANSACTIONS"}'
    signed = jwt.encode(
        {
            "iat": int(datetime.now(UTC).timestamp()),
            "request_body_sha256": hashlib.sha256(body).hexdigest(),
        },
        private_key,
        algorithm="ES256",
        headers={"kid": "key-one"},
    )
    client = PlaidClientWithVerificationKey(key)

    assert await client.verify_webhook(body=body, signed_jwt=signed) is True
    assert await client.verify_webhook(body=body + b" ", signed_jwt=signed) is False


@pytest.mark.anyio
async def test_plaid_sync_normalizes_private_financial_data_and_goals() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    settings = Settings(
        integration_token_encryption_key=Fernet.generate_key().decode(),
        plaid_client_id="plaid-client",
        plaid_secret="plaid-secret",
    )
    fake_plaid = FakePlaidClient()
    async with session_factory() as session:
        user = User(phone_number="+14155552671")
        session.add(user)
        await session.flush()
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.commit()

        link = await create_plaid_link_token(
            session,
            user_id=user.id,
            initiated_channel="web",
            settings=settings,
            plaid_client=fake_plaid,  # type: ignore[arg-type]
        )
        assert link.link_token == "link-sandbox-test"
        completed = await complete_plaid_link(
            session,
            exchange_token=link.exchange_token,
            public_token="public-sandbox-test",
            institution_id="ins_1",
            institution_name="Sandbox Bank",
            settings=settings,
            plaid_client=fake_plaid,  # type: ignore[arg-type]
        )
        assert "access-sandbox-test" not in completed.connection.credentials_ciphertext
        assert await session.scalar(select(func.count()).select_from(ScheduledTask)) == 1

    await sync_financial_connection(
        connection_id=completed.connection.id,
        settings=settings,
        notify_on_complete=True,
        plaid_client=fake_plaid,  # type: ignore[arg-type]
        session_factory=session_factory,
    )

    async with session_factory() as session:
        update_link = await create_plaid_link_token(
            session,
            user_id=user.id,
            initiated_channel="web",
            settings=settings,
            plaid_client=fake_plaid,  # type: ignore[arg-type]
            connection_id=completed.connection.id,
        )
        assert fake_plaid.link_requests[-1]["access_token"] == "access-sandbox-test"
        await complete_plaid_link(
            session,
            exchange_token=update_link.exchange_token,
            public_token="ignored-in-update-mode",
            institution_id=None,
            institution_name=None,
            settings=settings,
            plaid_client=fake_plaid,  # type: ignore[arg-type]
        )
        assert fake_plaid.exchange_calls == 1

    overview_tool = FinancialOverviewTool(session_factory=session_factory)
    overview = await overview_tool.execute(
        context=ToolContext(user_id=user.id, conversation_id=conversation.id),
        arguments={"days": 30},
    )
    assert overview["connected"] is True
    assert overview["accounts"][0]["current_balance"] == "2400.5000"
    assert overview["cash_flow"] == [
        {
            "currency": "USD",
            "outflows": "8.7500",
            "inflows": "1500.0000",
            "net_inflow": "1491.2500",
        }
    ]

    search_tool = SearchFinancialTransactionsTool(session_factory=session_factory)
    today = datetime.now(UTC).date().isoformat()
    search_result = await search_tool.execute(
        context=ToolContext(user_id=user.id, conversation_id=conversation.id),
        arguments={
            "date_from": today,
            "date_to": today,
            "query": "coffee",
            "limit": 10,
        },
    )
    assert search_result["count"] == 1
    assert search_result["transactions"][0]["merchant"] == "Daybreak Coffee"
    assert search_result["transactions"][0]["direction"] == "outflow"

    goal_tool = CreateFinancialGoalTool(session_factory=session_factory)
    goal_result = await goal_tool.execute(
        context=ToolContext(user_id=user.id, conversation_id=conversation.id),
        arguments={
            "title": "save for november trip",
            "target_amount": 3000,
            "currency": "USD",
            "target_date": (datetime.now(UTC).date() + timedelta(days=90)).isoformat(),
            "baseline_amount": 500,
            "proactive_checkins": True,
            "first_check_at": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
            "timezone": "Africa/Cairo",
        },
    )
    assert goal_result["proactive_checkins"] is True

    async with session_factory() as session:
        connection = await session.scalar(select(FinancialConnection))
        account = await session.scalar(select(FinancialAccount))
        goals = list((await session.scalars(select(FinancialGoal))).all())
        assert connection is not None and connection.sync_cursor == "cursor-one"
        assert account is not None and account.mask == "1234"
        assert await session.scalar(select(func.count()).select_from(FinancialTransaction)) == 2
        assert await session.scalar(select(func.count()).select_from(UserEvent)) == 1
        assert len(goals) == 1 and goals[0].schedule_id is not None
        assert await session.scalar(select(func.count()).select_from(ScheduledTask)) == 3

        disconnected = await disconnect_financial_connection(
            session,
            user_id=user.id,
            connection_id=completed.connection.id,
            settings=settings,
            plaid_client=fake_plaid,  # type: ignore[arg-type]
        )
        await session.commit()
        assert disconnected is True
        assert fake_plaid.removed_items == ["access-sandbox-test"]

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(FinancialConnection)) == 0
        assert await session.scalar(select(func.count()).select_from(FinancialAccount)) == 0
        assert await session.scalar(select(func.count()).select_from(FinancialTransaction)) == 0
    await engine.dispose()
