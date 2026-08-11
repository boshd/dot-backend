"""Scope generated-app records to composable modules.

Revision ID: 20260811_0021
Revises: 20260811_0020
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260811_0021"
down_revision: str | None = "20260811_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_MODULES = {
    "budget": ("expenses", "expenses"),
    "expense_splitter": ("expenses", "expenses"),
    "metric_tracker": ("metric", "metric"),
    "checklist": ("todos", "todos"),
}


def upgrade() -> None:
    op.add_column(
        "generated_app_records",
        sa.Column("module_id", sa.String(length=64), nullable=True),
    )

    connection = op.get_bind()
    apps = sa.table(
        "generated_apps",
        sa.column("id", sa.Uuid()),
        sa.column("template", sa.String()),
    )
    versions = sa.table(
        "generated_app_versions",
        sa.column("id", sa.Uuid()),
        sa.column("app_id", sa.Uuid()),
        sa.column("specification", sa.JSON()),
    )
    records = sa.table(
        "generated_app_records",
        sa.column("id", sa.Uuid()),
        sa.column("app_id", sa.Uuid()),
        sa.column("module_id", sa.String()),
    )

    app_templates = dict(connection.execute(sa.select(apps.c.id, apps.c.template)).all())
    for app_id, template in app_templates.items():
        module_id, _ = LEGACY_MODULES.get(template, ("main", "overview"))
        connection.execute(
            sa.update(records).where(records.c.app_id == app_id).values(module_id=module_id)
        )

    version_rows = connection.execute(
        sa.select(versions.c.id, versions.c.app_id, versions.c.specification)
    ).mappings()
    for row in version_rows:
        specification = dict(row["specification"] or {})
        if specification.get("schema_version") == 2 and isinstance(
            specification.get("modules"), list
        ):
            continue
        template = str(specification.get("template") or app_templates.get(row["app_id"]) or "")
        module_id, module_type = LEGACY_MODULES.get(template, ("main", "overview"))
        settings = dict(specification.get("settings") or {})
        module_settings = _legacy_module_settings(template, settings)
        capabilities = list(specification.get("capabilities") or [])
        specification.update(
            {
                "schema_version": 2,
                "modules": [
                    {
                        "id": module_id,
                        "type": module_type,
                        "title": _legacy_module_title(template),
                        "description": "",
                        "settings": module_settings,
                    }
                ],
                "capabilities": capabilities,
            }
        )
        connection.execute(
            sa.update(versions)
            .where(versions.c.id == row["id"])
            .values(specification=specification)
        )

    op.alter_column(
        "generated_app_records",
        "module_id",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    op.create_index(
        "ix_generated_app_records_app_module_kind",
        "generated_app_records",
        ["app_id", "module_id", "kind"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    workspace_count = connection.scalar(
        sa.text("SELECT COUNT(*) FROM generated_apps WHERE template = 'workspace'")
    )
    if workspace_count:
        raise RuntimeError("Cannot downgrade while composable workspace apps exist")

    versions = sa.table(
        "generated_app_versions",
        sa.column("id", sa.Uuid()),
        sa.column("specification", sa.JSON()),
    )
    version_rows = connection.execute(
        sa.select(versions.c.id, versions.c.specification)
    ).mappings()
    for row in version_rows:
        specification = dict(row["specification"] or {})
        specification.pop("modules", None)
        specification["schema_version"] = 1
        connection.execute(
            sa.update(versions)
            .where(versions.c.id == row["id"])
            .values(specification=specification)
        )

    op.drop_index(
        "ix_generated_app_records_app_module_kind",
        table_name="generated_app_records",
    )
    op.drop_column("generated_app_records", "module_id")


def _legacy_module_title(template: str) -> str:
    return {
        "budget": "Expenses",
        "expense_splitter": "Shared expenses",
        "metric_tracker": "Progress",
        "checklist": "To dos",
    }.get(template, "Overview")


def _legacy_module_settings(template: str, settings: dict[str, object]) -> dict[str, object]:
    if template == "budget":
        return {
            "currency": settings.get("currency") or "USD",
            "budget": settings.get("monthly_budget"),
            "mode": "personal",
        }
    if template == "expense_splitter":
        return {
            "currency": settings.get("currency") or "USD",
            "budget": None,
            "mode": "split",
        }
    if template == "metric_tracker":
        return {
            "unit": settings.get("unit") or "units",
            "target": settings.get("target"),
            "direction": settings.get("direction") or "decrease",
        }
    if template == "checklist":
        return {"show_completed": True}
    return {}
