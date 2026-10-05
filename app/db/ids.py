"""Atomic id allocation (G-DATA-01)."""
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import models as m


async def next_id(s: AsyncSession, prefix: str, width: int) -> str:
    row = (await s.execute(update(m.IdSequence).where(m.IdSequence.name == prefix).values(value=m.IdSequence.value + 1)
                           .returning(m.IdSequence.value))).first()
    if row is None:  # first use on a database that was not seeded with sequences: start from the existing max
        from sqlalchemy import func, select
        tbl = {"DSP": m.Dispute, "TCK": m.Ticket, "HRQ": m.HumanReview, "VER": m.Verification, "TXN": m.Transaction, "CARD": m.Card}[prefix]
        cur = (await s.execute(select(func.count()).select_from(tbl))).scalar_one()
        s.add(m.IdSequence(name=prefix, value=cur + 1))
        await s.flush()
        return f"{prefix}-{cur + 1:0{width}d}"
    return f"{prefix}-{row[0]:0{width}d}"
