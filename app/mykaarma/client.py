"""myKaarma API client.

HTTP Basic auth against https://api.mykaarma.com. Credentials are per-dealer
(loaded from the mykaarma_dealers table, falling back to env for the sandbox)
so more stores can be added without a redeploy.

The single most important behaviour in this file: **scope detection.** Several
endpoints we need (repair orders, dealer orders) return HTTP 200 with an HTML
login page instead of JSON when our account lacks the scope. A naive client
would treat that 200 as success and then choke parsing HTML as JSON. So every
call goes through `_post` / `_get`, which inspect the content-type and raise
`ScopeNotGrantedError` on non-JSON — turning a silent misfire into an honest,
catchable signal the connector can fall back from.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Optional

import httpx

BASE_URL = "https://api.mykaarma.com"


class MyKaarmaError(Exception):
    """Any myKaarma call that did not succeed as JSON."""


class ScopeNotGrantedError(MyKaarmaError):
    """Endpoint returned non-JSON (HTML login page) => our account lacks the scope.

    This is the expected state today for repair-order / dealer-order endpoints in
    the sandbox. The connector catches it and falls back to the CSV importer.
    """


class MyKaarmaAuthError(MyKaarmaError):
    """401/403 — bad or unauthorized credentials."""


@dataclass(frozen=True)
class MyKaarmaCreds:
    username: str
    password: str
    dealer_uuid: str
    department_uuid: str

    @property
    def basic_token(self) -> str:
        raw = f"{self.username}:{self.password}".encode()
        return base64.b64encode(raw).decode()


class MyKaarmaClient:
    def __init__(self, creds: MyKaarmaCreds, timeout: float = 30.0):
        self.creds = creds
        self._timeout = timeout

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Basic {self.creds.basic_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # --------------------------------------------------------------------- #
    # low-level request with scope detection                                #
    # --------------------------------------------------------------------- #

    def _handle(self, resp: httpx.Response, label: str) -> Any:
        body_lc = resp.text.lower()
        if resp.status_code in (401, 403):
            # The documented Order v2 API returns a clean JSON 403 with
            # "ApiScope does not exist" when the scope isn't provisioned — the
            # authoritative "not granted" signal. Distinguish that from a plain
            # bad-credentials rejection.
            if "apiscope" in body_lc or "scope does not" in body_lc or "scope not" in body_lc:
                raise ScopeNotGrantedError(
                    f"{label}: HTTP 403 — myKaarma reports the API scope is not provisioned "
                    f"for this account. Request the scope from myKaarma to enable it."
                )
            raise MyKaarmaAuthError(f"{label}: HTTP {resp.status_code} — credentials rejected")
        ctype = resp.headers.get("content-type", "")
        if "json" not in ctype.lower():
            # HTTP 200 + HTML == a non-API/legacy route (falls through to the
            # web portal). Also treated as "not available to us".
            raise ScopeNotGrantedError(
                f"{label}: HTTP {resp.status_code} returned {ctype or 'no content-type'} "
                f"(not JSON) — not a live API route for this account"
            )
        if resp.status_code >= 400:
            raise MyKaarmaError(f"{label}: HTTP {resp.status_code} — {resp.text[:200]}")
        return resp.json()

    def _post(self, path: str, body: dict, label: str, params: Optional[dict] = None) -> Any:
        with httpx.Client(timeout=self._timeout) as c:
            resp = c.post(f"{BASE_URL}{path}", headers=self._headers, json=body, params=params)
        return self._handle(resp, label)

    def _get(self, path: str, label: str, params: Optional[dict] = None) -> Any:
        with httpx.Client(timeout=self._timeout) as c:
            resp = c.get(f"{BASE_URL}{path}", headers=self._headers, params=params)
        return self._handle(resp, label)

    # --------------------------------------------------------------------- #
    # health                                                                #
    # --------------------------------------------------------------------- #

    def ping(self) -> dict:
        """Cheapest authenticated call that returns JSON — the opcodes search.

        Confirms base URL + credentials + JSON path are all good.
        """
        d = self.search_opcodes(result_size=1)
        return {"ok": True, "opcode_total": d.get("totalCount")}

    # --------------------------------------------------------------------- #
    # WORKING endpoints (verified live in the sandbox)                       #
    # --------------------------------------------------------------------- #

    def search_opcodes(self, result_size: int = 50, start: int = 0) -> dict:
        """POST (not GET). Do NOT send onlineSchedulerVisibility in the sandbox —
        it filters everything out."""
        return self._post(
            f"/opcodes/v1/dealers/{self.creds.dealer_uuid}/operations/searches",
            {"resultSize": result_size, "startPosition": start, "getTotalCount": True},
            "search_opcodes",
        )

    def get_availability(self, dates: list[str], operation_uuids: Optional[list[str]] = None) -> dict:
        """Real open slots + the advisor (DEALER_ASSOCIATE) list + dealer hours."""
        return self._post(
            f"/appointment/v2/department/{self.creds.department_uuid}/availability",
            {
                "dates": dates,
                "selectedOperationUuidSet": operation_uuids or [],
                "selectedAvailabilityAttributes": {},
                "allAvailabilityAttributes": {},
            },
            "get_availability",
            params={"refreshSelectionState": "true"},
        )

    def save_customer(self, customer: dict, vehicles: Optional[list[dict]] = None) -> dict:
        """Create/dedupe a customer. Phone MUST be E.164 (+16305550147)."""
        return self._post(
            f"/customer/v2/department/{self.creds.department_uuid}/customer",
            {"customer": customer, "searchForDuplicate": True, "vehicles": vehicles or []},
            "save_customer",
        )

    # --------------------------------------------------------------------- #
    # RO (Order v2) — the DOCUMENTED endpoint. Read-by-UUID.                  #
    # Confirmed real: returns a clean JSON 403 "ApiScope does not exist"      #
    # today, i.e. the endpoint exists and only the scope grant is missing.    #
    # --------------------------------------------------------------------- #

    # A nil UUID: if the scope were granted this returns "order not found"
    # (JSON, endpoint works); without it, a 403 ApiScope. Either way it is a
    # valid, side-effect-free scope probe.
    _NIL_UUID = "00000000000000000000000000000000"

    def get_order(self, order_uuid: str) -> dict:
        """GET /order/v2/department/{dept}/global_order/{orderUuid} — the full
        DMS-synced repair order (header + jobs[] + parts). Documented in the v2
        Integration Contract. Raises ScopeNotGrantedError until myKaarma grants
        the repair-order scope."""
        return self._get(
            f"/order/v2/department/{self.creds.department_uuid}/global_order/{order_uuid}",
            "get_order",
        )

    def probe_ro_scope(self) -> bool:
        """True if the repair-order scope is granted.

        Probes Order v2 with a nil UUID. Interpreting the response:
          * 403 "ApiScope does not exist"  -> scope NOT granted
          * 400 "ORDER_NOT_FOUND"          -> scope IS granted (endpoint answered
            us properly; that UUID simply doesn't exist, which is expected)
        """
        try:
            self.get_order(self._NIL_UUID)
            return True
        except ScopeNotGrantedError:
            return False
        except MyKaarmaError as e:
            # A business-level error (ORDER_NOT_FOUND) means the endpoint is
            # open to us — that IS the granted signal.
            if "ORDER_NOT_FOUND" in str(e).upper() or "NOT FOUND WITH UUID" in str(e).upper():
                return True
            raise
