import argparse
import asyncio

from benji_api.db.session import async_session_factory
from benji_api.services.user_reset import (
    UserResetPlan,
    build_user_reset_plan,
    execute_user_reset,
)
from benji_api.services.users import normalize_user_identifier


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview or delete all Dot data associated with a phone or email."
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--identifier", help="Phone number or email used to message Dot")
    target.add_argument(
        "--phone",
        help="Deprecated alias for --identifier; phone number in international format",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform the deletion; without this flag the command is read-only",
    )
    parser.add_argument(
        "--confirm-identifier",
        help="Required with --execute and must normalize to the requested identifier",
    )
    parser.add_argument("--confirm-phone", help=argparse.SUPPRESS)
    return parser.parse_args()


def _print_plan(plan: UserResetPlan) -> None:
    print(f"identifier:         {plan.normalized_identifier}")
    print(f"identifier kind:    {plan.identifier_kind}")
    print(f"user:               {int(plan.user_id is not None)}")
    print(f"user identifiers:   {len(plan.user_identifier_ids)}")
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
    print(f"total records:       {plan.total_records}")


async def _run(args: argparse.Namespace) -> None:
    requested_value = args.identifier or args.phone
    if requested_value is None:  # pragma: no cover - argparse enforces this
        raise SystemExit("--identifier is required")
    if args.execute:
        confirmed_value = args.confirm_identifier or args.confirm_phone
        if not confirmed_value:
            raise SystemExit("--confirm-identifier is required with --execute")
        try:
            confirmed = normalize_user_identifier(confirmed_value)
            requested = normalize_user_identifier(requested_value)
        except ValueError as error:
            raise SystemExit(str(error)) from error
        if confirmed != requested:
            raise SystemExit("--confirm-identifier does not match --identifier")

    async with async_session_factory() as session:
        plan = await build_user_reset_plan(session, requested_value)
        _print_plan(plan)

        if not args.execute:
            print("\ndry run only; no data was deleted")
            return

        await execute_user_reset(session, plan)
        await session.commit()
        print(f"\ndeleted {plan.total_records} records for {plan.normalized_identifier}")


def main() -> None:
    args = _parse_args()
    try:
        asyncio.run(_run(args))
    except ValueError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
