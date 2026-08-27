"""Provision all McGrath stores with their own myKaarma department UUIDs.

One shared login covers every store; what separates them is each store's own
dealer_uuid + Service department_uuid, pulled live from the configurations
endpoint. Upserts one Dealer row + one mykaarma_dealers creds row per store.
Idempotent — safe to re-run. Run:

    python provision_stores.py
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Dealer, DealerSettings, MyKaarmaDealer
from app.mykaarma.client import MyKaarmaClient, MyKaarmaCreds

settings = get_settings()

# business `name` in the configurations response -> (stable dealer_key, display name)
TARGETS = [
    ("McGrath Honda of St. Charles",  "mcgrath_honda_stcharles",   "McGrath Honda of St. Charles"),
    ("McGrath KIA of St. Charles",    "mcgrath_kia_stcharles",     "McGrath Kia of St. Charles"),
    ("McGrath Honda of Elgin",        "mcgrath_honda_elgin",       "McGrath Honda of Elgin"),
    ("McGrath Acura Morton Grove",    "mcgrath_acura_mortongrove", "McGrath Acura of Morton Grove"),
    ("McGrath Acura of Libertyville", "mcgrath_acura_libertyville","McGrath Acura of Libertyville"),
    ("McGrath Volvo Cars Barrington", "mcgrath_volvo_barrington",  "McGrath Volvo Cars Barrington"),
    ("Audi Morton Grove",             "audi_mortongrove",          "Audi Morton Grove"),
]
TZ = "America/Chicago"


def _service_dept(business: dict) -> str | None:
    for d in business.get("departments") or []:
        if (d.get("name") or "").strip().lower() == "service":
            return d.get("uuid")
    return None


async def main() -> None:
    creds = MyKaarmaCreds(
        username=settings.mykaarma_username,
        password=settings.mykaarma_password,
        dealer_uuid=settings.mykaarma_dealer_uuid or "-",
        department_uuid=settings.mykaarma_department_uuid or "-",
    )
    data = MyKaarmaClient(creds).get_configurations()
    by_name = {(b.get("name") or "").strip(): b for b in data.get("enabledBusinesses") or []}

    async with SessionLocal() as session:
        for biz_name, key, display in TARGETS:
            biz = by_name.get(biz_name)
            if biz is None:
                print(f"SKIP  {display}: not found in configurations")
                continue
            dealer_uuid = biz.get("uuid")
            dept_uuid = _service_dept(biz)
            if not dept_uuid:
                print(f"SKIP  {display}: no Service department")
                continue

            dealer = (
                await session.execute(select(Dealer).where(Dealer.dealer_key == key))
            ).scalar_one_or_none()
            if dealer is None:
                dealer = (
                    await session.execute(select(Dealer).where(Dealer.name == display))
                ).scalar_one_or_none()
            if dealer is None:
                dealer = Dealer(name=display, timezone=TZ, dealer_key=key)
                session.add(dealer)
                await session.flush()
                session.add(DealerSettings(dealer_id=dealer.id))
                tag = "created"
            else:
                dealer.dealer_key = key
                dealer.name = display
                dealer.timezone = dealer.timezone or TZ
                has = (
                    await session.execute(
                        select(DealerSettings).where(DealerSettings.dealer_id == dealer.id)
                    )
                ).scalar_one_or_none()
                if has is None:
                    session.add(DealerSettings(dealer_id=dealer.id))
                tag = "updated"

            myk = await session.get(MyKaarmaDealer, dealer.id)
            if myk is None:
                myk = MyKaarmaDealer(dealer_id=dealer.id)
                session.add(myk)
            myk.username = creds.username
            myk.password = creds.password
            myk.dealer_uuid = dealer_uuid
            myk.department_uuid = dept_uuid
            myk.enabled = True
            myk.ro_scope_granted = True

            print(f"{tag:8} {display:34} key={key:26} dept={dept_uuid[:12]}…")

        await session.commit()
    print("\nDone. All matched stores provisioned with their own department UUIDs.")


if __name__ == "__main__":
    asyncio.run(main())
