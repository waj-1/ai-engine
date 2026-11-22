"""Inbound support tools: the verification gate and every once-only action
are enforced in code, not just in the prompt."""
from __future__ import annotations

import json
from datetime import date

import pytest

from runtime.tools.agents_demo import support

LAYLA = dict(account_number="41203317", date_of_birth="1991-03-14")


@pytest.fixture
def st():
    return support.new_state()


def _verified(acct="41203317", dob=None, id4=None):
    s = support.new_state()
    from runtime.tools.agents_demo import data

    c = data.customer(acct)
    out = support.verify_customer(s, account_number=acct, date_of_birth=dob or c["dob"],
                                  id_last4=id4)
    assert out["ok"], out
    return s


# ------------------------------------------------------------- verification
def test_account_tools_refuse_before_verification(st):
    for fn in (support.run_line_diagnostics, support.explain_bill, support.request_refund,
               support.book_technician):
        assert fn(st)["error"] == "not_verified"
    assert support.change_service(st, action="pause", months=1)["error"] == "not_verified"
    assert support.open_ticket(st, category="billing", summary="x")["error"] == "not_verified"


def test_verify_with_account_and_dob_returns_summary(st):
    out = support.verify_customer(st, **LAYLA)
    assert out["ok"] and out["name"] == "Layla Haddad"
    assert out["registered_mobile_last4"] == "0317"
    assert st["verified"] == "41203317"


def test_verify_accepts_mobile_and_id_last4_and_dd_mm_yyyy():
    s = support.new_state()
    assert support.verify_customer(s, mobile="+971 50 124 0317", id_last4="5521")["ok"]
    s2 = support.new_state()
    assert support.verify_customer(s2, account_number="٤١٢٠٣٣١٧", date_of_birth="14/03/1991")["ok"]


def test_mismatch_never_says_which_part_and_locks_after_three(st):
    for wrong in ("1990-01-01", "1990-01-02", "1990-01-03"):   # a repeat is not a new guess
        out = support.verify_customer(st, account_number="41203317", date_of_birth=wrong)
        assert out["error"] == "details_do_not_match"
        assert "date of birth" not in out["detail"].lower().split("never say")[0]
    assert support.verify_customer(st, **LAYLA)["error"] == "too_many_attempts"
    assert st["verified"] is None


def test_missing_parts_are_asked_for(st):
    assert support.verify_customer(st, date_of_birth="1991-03-14")["error"] == "missing_identifier"
    assert support.verify_customer(st, account_number="41203317")["error"] == "missing_second_factor"
    assert support.verify_customer(st, account_number="4120331", date_of_birth="1991-03-14")[
        "error"] == "invalid_account_number"


def test_third_party_with_holders_details_is_refused(st):
    out = support.verify_customer(st, account_number="71316604", date_of_birth="1956-02-28",
                                  caller_name="Tariq")
    assert out["error"] == "not_account_holder"
    assert st["verified"] is None


def test_outage_check_is_public_and_area_matched(st):
    out = support.check_outage(st, area="Al Barsha")
    assert out["ok"] and out["outage"] and out["outages"][0]["id"] == "OUT-4471"
    assert support.check_outage(st, area="Al Faisaliyah")["outage"] is False
    assert support.check_outage(st, area="Atlantis")["error"] == "unknown_area"


def test_outage_uses_verified_customers_area():
    s = _verified("41207749")  # Omar, Al Barsha
    assert support.check_outage(s)["outage"] is True


# ----------------------------------------------------------------- actions
def test_diagnostics_verdicts():
    assert support.run_line_diagnostics(_verified())["verdict"] == "wifi_congestion"
    no_sync = support.run_line_diagnostics(_verified("71308821"))
    assert no_sync["needs_technician"] and no_sync["visit_charge"].startswith("free")


def test_bill_explains_change_vs_previous():
    out = support.explain_bill(_verified("71304412"))
    assert out["total"] == "583 riyals"
    assert out["change_vs_previous"] == "+75 riyals"
    assert any("Roaming day pass" in line for line in out["lines"])
    assert out["due_date"] == "20 October 2026"


def test_refund_only_for_a_duplicate_and_only_once():
    assert support.request_refund(_verified())["error"] == "no_duplicate_found"
    s = _verified("41209908")
    first = support.request_refund(s, refund_to="original_payment_method")
    assert first["ok"] and first["amount"] == "349 dirhams" and first["reference"].startswith("REF-")
    again = support.request_refund(s)
    assert again["error"] == "already_requested" and again["reference"] == first["reference"]


def test_technician_slots_then_book_once():
    s = _verified("71308821")
    listed = support.book_technician(s, today=date(2026, 10, 7))
    assert listed["booked"] is False and len(listed["available_slots"]) == 6
    slot = listed["available_slots"][0]["slot_id"]
    assert support.book_technician(s, slot_id="2099-01-01-AM", today=date(2026, 10, 7))["error"] == "slot_unavailable"
    booked = support.book_technician(s, slot_id=slot, today=date(2026, 10, 7))
    assert booked["ok"] and booked["reference"].startswith("VIS-")
    assert support.book_technician(s, slot_id=slot, today=date(2026, 10, 7))["error"] == "already_booked"


def test_slots_skip_the_weekend():
    s = _verified("71308821")
    slots = support.book_technician(s, today=date(2026, 10, 8))["available_slots"]  # a Thursday
    assert all("Friday" not in x["when"] and "Saturday" not in x["when"] for x in slots)


def test_service_change_quotes_then_confirms_once():
    s = _verified("71312075")  # Faisal: 7 months left on 339
    quote = support.change_service(s, action="cancel")
    assert quote["done"] is False
    assert quote["quote"]["early_termination_fee"] == "1,186.50 riyals"
    assert "pause" in quote["quote"]["alternative"]
    paused = support.change_service(s, action="pause", months=3, confirm=True)
    assert paused["done"] and paused["summary"]["total_fee"] == "75 riyals"
    assert support.change_service(s, action="pause", months=1, confirm=True)["error"] == "already_done"
    assert support.change_service(_verified("71312075"), action="pause", months=4)["error"] == "invalid_months"


def test_relocation_checks_coverage_and_notice():
    s = _verified("41211560")
    out = support.change_service(s, action="relocate", new_area="Al Reem Island", move_date="2026-11-25",
                                 today=date(2026, 10, 7))
    assert out["quote"]["fiber_available"] is True
    assert out["quote"]["relocation_fee"].startswith("free")
    assert support.change_service(s, action="relocate", new_area="Riyadh Al Malqa")["error"] == "other_country"
    assert support.change_service(s, action="relocate", new_area="Al Reem", confirm=True)["error"] == "missing_move_date"


def test_esim_and_wrong_service_type():
    s = _verified("41215532")  # mobile only
    out = support.change_service(s, action="esim")
    assert out["quote"]["fee"].startswith("free") and "m•••@mail.example" in out["quote"]["how"]
    assert support.change_service(s, action="pause", months=1)["error"] == "no_home_service"


def test_handoff_works_unverified_and_once(st):
    first = support.handoff_to_human(st, summary="Caller asks for a person")
    assert first["ok"] and first["verified"] is False
    assert support.handoff_to_human(st, summary="again")["error"] == "already_handed_off"


def test_langchain_tools_share_state_and_render_arabic():
    st = support.new_state()
    tools = {t.name: t for t in support.create_support_tools({"call_state": st, "locale": "ar"})}
    assert set(tools) >= {"support_verify_customer", "support_check_outage", "support_handoff_to_human"}
    out = json.loads(tools["support_verify_customer"].invoke({"account_number": "71304412", "id_last4": "1187"}))
    assert out["ok"] and out["name"] == "نورة القحطاني"
    bill = json.loads(tools["support_explain_bill"].invoke({}))
    assert bill["total"] == "583 ريال"
