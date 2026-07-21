"""SQLAlchemy declarative base — guide Part 3.

One shared ``Base`` for every module's models. Each model sets its own module's
Postgres schema explicitly via ``__table_args__ = ({"schema": "<module>"},)``
(database-design.md §1). No cross-schema ``ForeignKey`` is ever declared — an
inter-schema reference is a plain ``UUID`` column with no FK (database-design.md §5).
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Deterministic constraint/index names so Alembic migrations (and their
# downgrades) are stable and reviewable across databases. This is a NAMING
# convention only — it changes no column and no table's shape.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base shared by all module ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
