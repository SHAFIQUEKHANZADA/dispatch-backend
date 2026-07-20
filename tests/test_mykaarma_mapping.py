"""Tests for the myKaarma Order v2 -> 3D Dispatch mapping.

The mapping is pure, so it can be tested against representative payloads without
a network or a database. If myKaarma changes a field name, these fail loudly
rather than quietly producing wrong ROs on the dispatch board.
"""

from __future__ import annotations

from datetime import timezone

from app.mykaarma.mapping import (
    line_waiting_on_parts,
    map_labor_type,
    map_order,
    map_status,
)


def order_payload(**overrides) -> dict:
    """A representative Order v2 global_order response."""
    header = {
        "roNumber": "4471",
        "dmsStatus": "READY",
        "promisedDate": "2026-07-20",
        "promisedTime": "16:00",
        "createDate": "2026-07-20",
        "createTime": "08:45",
        "waiter": True,
        "soldHours": 2.5,
        "actualHours": 1.9,
        "advisorNumber": "A-01",
        "advisorName": "Kevin",
        "appointmentNumber": "APPT-9912",
        "mileageIn": 88700,
    }
    header.update(overrides.pop("header", {}))
    payload = {
        "orderReadChecksum": "chk-abc123",
        "order": {
            "uuid": "order-uuid-1",
            "header": header,
            "customer": {"uuid": "cust-uuid-1"},
            "vehicle": {
                "uuid": "veh-uuid-1",
                "vin": "1hgcm82633a004352",
                "year": 2019,
                "make": "Honda",
                "model": "Odyssey",
            },
            "jobs": overrides.pop(
                "jobs",
                [
                    {
                        "laborOpCode": "ACDIAG",
                        "laborOpCodeDesc": "A/C blowing warm",
                        "laborType": "C",
                        "soldHours": 1.0,
                        "techNo": "T101",
                        "dispatchLineStatus": "ASSIGNED",
                        "parts": [],
                    },
                    {
                        "laborOpCode": "ACRCHG",
                        "laborOpCodeDesc": "Evac & recharge",
                        "laborType": "W",
                        "soldHours": 1.5,
                        "techNo": "T101",
                        "parts": [{"quantityOrdered": 2, "quantitySold": 2}],
                    },
                ],
            ),
        },
    }
    payload.update(overrides)
    return payload


# --------------------------- header mapping -------------------------------- #


def test_maps_core_header_fields():
    ro = map_order(order_payload())
    assert ro.ro_number == "4471"
    assert ro.order_uuid == "order-uuid-1"
    assert ro.status == "READY_TO_DISPATCH"
    assert ro.vin == "1HGCM82633A004352"          # upper-cased
    assert ro.vehicle_year == 2019
    assert ro.vehicle_make == "Honda"
    assert ro.mileage == 88700
    assert ro.advisor_id == "A-01"
    assert ro.appointment_number == "APPT-9912"
    assert ro.customer_uuid == "cust-uuid-1"
    assert ro.read_checksum == "chk-abc123"        # needed for future write-back


def test_promise_combines_date_and_time_as_utc():
    ro = map_order(order_payload())
    assert ro.promise_at is not None
    assert ro.promise_at.tzinfo == timezone.utc
    assert (ro.promise_at.hour, ro.promise_at.minute) == (16, 0)
    assert ro.written_at is not None and ro.written_at.hour == 8


def test_waiter_becomes_the_waiting_flag():
    assert "WAITING" in map_order(order_payload()).flags
    quiet = map_order(order_payload(header={"waiter": False}))
    assert "WAITING" not in quiet.flags


def test_unparseable_promise_is_none_not_a_guess():
    ro = map_order(order_payload(header={"promisedDate": "not-a-date", "promisedTime": None}))
    assert ro.promise_at is None   # a wrong promise time is worse than none


# --------------------------- status mapping -------------------------------- #


def test_status_map_covers_the_board_buckets():
    assert map_status({"dmsStatus": "READY"}) == "READY_TO_DISPATCH"
    assert map_status({"dmsStatus": "PENDING_AUTH"}) == "PENDING_AUTHORIZATION"
    assert map_status({"dmsStatus": "WAITING PARTS"}) == "WAITING_ON_PARTS"
    assert map_status({"dmsStatus": "WIP"}) == "IN_PROGRESS"
    assert map_status({"dmsStatus": "CLOSED"}) == "COMPLETED"


def test_unknown_status_falls_back_to_open_not_dispatchable():
    """An unrecognised DMS status must never land work on the dispatch board."""
    assert map_status({"dmsStatus": "SOME_NEW_STATUS"}) == "OPEN"
    assert map_status({}) == "OPEN"


def test_dms_status_wins_over_mykaarma_status():
    assert map_status({"dmsStatus": "WIP", "status": "READY"}) == "IN_PROGRESS"


# --------------------------- jobs / lines ---------------------------------- #


def test_lines_carry_opcode_hours_and_tech():
    ro = map_order(order_payload())
    assert len(ro.lines) == 2
    assert ro.lines[0].op_code == "ACDIAG"
    assert ro.lines[0].labor_type == "CP"
    assert ro.lines[1].labor_type == "WARRANTY"
    assert ro.lines[0].flagged_hours == 1.0
    assert ro.dms_tech_nos == ["T101"]


def test_est_hours_falls_back_to_sum_of_lines():
    ro = map_order(order_payload(header={"soldHours": 0}))
    assert ro.est_hours == 2.5     # 1.0 + 1.5


def test_labor_type_normalisation():
    assert map_labor_type("C") == "CP"
    assert map_labor_type("customer pay") == "CP"
    assert map_labor_type("W") == "WARRANTY"
    assert map_labor_type("Internal") == "INTERNAL"
    assert map_labor_type("") is None


# --------------------------- parts / waiting ------------------------------- #


def test_line_is_parts_blocked_when_ordered_exceeds_sold():
    assert line_waiting_on_parts({"parts": [{"quantityOrdered": 2, "quantitySold": 0}]}) is True
    assert line_waiting_on_parts({"parts": [{"quantityOrdered": 2, "quantitySold": 2}]}) is False
    assert line_waiting_on_parts({"parts": []}) is False


def test_parts_block_overrides_a_ready_status():
    """You cannot dispatch work whose parts have not arrived, whatever the
    DMS header claims."""
    payload = order_payload(
        jobs=[
            {
                "laborOpCode": "BRKFRT",
                "laborOpCodeDesc": "Front brakes",
                "laborType": "C",
                "soldHours": 2.0,
                "parts": [{"quantityOrdered": 4, "quantitySold": 0}],
            }
        ]
    )
    ro = map_order(payload)
    assert ro.status == "WAITING_ON_PARTS"


def test_missing_jobs_does_not_crash():
    payload = order_payload(jobs=[])
    ro = map_order(payload)
    assert ro.lines == []
    assert ro.dms_tech_nos == []
