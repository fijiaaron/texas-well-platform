#!/usr/bin/env python3
"""Regenerates data/schema.sql from the SQLAlchemy models in data/orm.py.

This is a documentation/reference artifact and a fast way to stand up a
database by hand (`psql -f data/schema.sql`) — the source of truth for the
actual schema is always data/orm.py. Run this after changing orm.py, don't
hand-edit schema.sql.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from data.orm import Base

HEADER = """-- Generated from data/orm.py by scripts/generate_schema_sql.py — do not hand-edit.
-- Run: python3 scripts/generate_schema_sql.py > data/schema.sql

CREATE EXTENSION IF NOT EXISTS postgis;
"""


def main() -> None:
    statements = [HEADER]
    for table in Base.metadata.sorted_tables:
        if table.name == "spatial_ref_sys":  # owned by the postgis extension itself
            continue
        statements.append(str(CreateTable(table).compile(dialect=postgresql.dialect())).strip() + ";")
        for index in table.indexes:
            statements.append(str(CreateIndex(index).compile(dialect=postgresql.dialect())).strip() + ";")
    print("\n\n".join(statements))


if __name__ == "__main__":
    main()
