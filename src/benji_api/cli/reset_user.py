import argparse
import asyncio

from benji_api.db.session import async_session_factory
from benji_api.schemas.phone import normalize_phone_number
from benji_api.services.user_reset import (
    UserResetPlan,
    build_user_reset_plan,
    execute_user_reset,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview or delete all local Dot data associated with a phone number."
    )
    parser.add_argument("--phone", required=True, help="Phone number in international format")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform the deletion; without this flag the command is read-only",
    )
    parser.add_argument(
        "--confirm-phone",
        help="Required with --execute and must resolve to the same E.164 phone number",
    )
    return parser.parse_args()


def _print_plan(plan: UserResetPlan) -> None:
    print(f"phone:              {plan.normalized_phone}")
    print(f"user:               {int(plan.user_id is not None)}")
    print(f"conversations:      {len(plan.conversation_ids)}")
    print(f"group memberships:  {len(plan.conversation_member_ids)}")
    print(f"group invites:      {len(plan.conversation_invite_ids)}")
    print(f"generated apps:     {len(plan.generated_app_ids)}")
    print(f"app versions:       {len(plan.generated_app_version_ids)}")
    print(f"app records:        {len(plan.generated_app_record_ids)}")
    print(f"auth identities:    {len(plan.auth_identity_ids)}")
    print(f"integration accounts: {len(plan.integration_account_ids)}")
    print(f"integration grants: {len(plan.integration_grant_ids)}")
    print(f"integration auth states: {len(plan.integration_oauth_state_ids)}")
    print(f"integration links: {len(plan.integration_connect_link_ids)}")
    print(f"integration subscriptions: {len(plan.integration_subscription_ids)}")
    print(f"financial connections: {len(plan.financial_connection_ids)}")
    print(f"financial accounts: {len(plan.financial_account_ids)}")
    print(f"financial transactions: {len(plan.financial_transaction_ids)}")
    print(f"financial goals: {len(plan.financial_goal_ids)}")
    print(f"scheduled tasks: {len(plan.scheduled_task_ids)}")
    print(f"channel bindings:   {len(plan.channel_ids)}")
    print(f"messages:           {len(plan.message_ids)}")
    print(f"message deliveries: {len(plan.delivery_ids)}")
    print(f"agent runs:         {len(plan.agent_run_ids)}")
    print(f"agent tool calls:   {len(plan.tool_call_ids)}")
    print(f"user events:        {len(plan.user_event_ids)}")
    print(f"memory jobs:        {len(plan.memory_job_ids)}")
    print(f"memory episodes:    {len(plan.memory_episode_ids)}")
    print(f"memory entities:    {len(plan.memory_entity_ids)}")
    print(f"memory facts:       {len(plan.memory_fact_ids)}")
    print(f"memory evidence:    {len(plan.memory_evidence_ids)}")
    print(f"webhook events:     {len(plan.webhook_event_ids)}")
    print(f"total local records: {plan.total_records}")


async def _run(args: argparse.Namespace) -> None:
    if args.execute:
        if not args.confirm_phone:
            raise SystemExit("--confirm-phone is required with --execute")
        try:
            confirmed_phone = normalize_phone_number(args.confirm_phone)
        except ValueError as error:
            raise SystemExit(str(error)) from error

        requested_phone = normalize_phone_number(args.phone)
        if confirmed_phone != requested_phone:
            raise SystemExit("--confirm-phone does not match --phone")

    async with async_session_factory() as session:
        plan = await build_user_reset_plan(session, args.phone)
        _print_plan(plan)

        if not args.execute:
            print("\ndry run only; no data was deleted")
            return

        await execute_user_reset(session, plan)
        await session.commit()
        print(f"\ndeleted {plan.total_records} local records for {plan.normalized_phone}")


def main() -> None:
    args = _parse_args()
    try:
        asyncio.run(_run(args))
    except ValueError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
