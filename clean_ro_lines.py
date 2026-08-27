"""Remove consent/notification boilerplate RO lines already stored (TCPA, payment
notices, signature blocks), so they stop cluttering the RO cards. Future syncs
filter them at ingest; this cleans what's already in the DB. Also lists any
technician named 'Nelson' so we can confirm before deactivating.

    python clean_ro_lines.py
"""

from __future__ import annotations

import asyncio

from sqlalchemy import delete, func, or_, select

from app.db import SessionLocal
from app.models import ROLine, Technician
from app.mykaarma.mapping import _BOILERPLATE_OPCODES, _BOILERPLATE_PHRASES


async def main() -> None:
    async with SessionLocal() as s:
        before = (await s.execute(select(func.count()).select_from(ROLine))).scalar()
        conds = [ROLine.op_code.in_(list(_BOILERPLATE_OPCODES))]
        for p in _BOILERPLATE_PHRASES:
            conds.append(ROLine.description.ilike(f"%{p}%"))
        await s.execute(delete(ROLine).where(or_(*conds)))
        await s.commit()
        after = (await s.execute(select(func.count()).select_from(ROLine))).scalar()
        print(f"RO lines: {before} -> {after}  (removed {before - after} boilerplate lines)")

        nelsons = list(
            (await s.execute(select(Technician).where(Technician.name.ilike("%nelson%")))).scalars()
        )
        print(f"\nTechnicians matching 'Nelson': {len(nelsons)}")
        for t in nelsons:
            print(f"  {t.name}  team={t.team}  active={t.active}  id={t.id}")


if __name__ == "__main__":
    asyncio.run(main())
