"""Outbound collection + sales: figures computed in code, the verification
gate (text mode), the once-only send yes-gate (both modes), and the case
file / direct voice tools."""
from __future__ import annotations

from datetime import date

import pytest

from runtime.tools.agents_demo import collection, common, data, sales


def _case(agent, cid):
    return data.resolve_case(agent, cid)


# ----------------------------------------------------------------- records
@pytest.mark.parametrize("cid,remaining,status", [
    ("C01", 723, "open"), ("C02", 303, "partial"), ("C03", 0, "paid"), ("C04", 474, "open"),
    ("C05", 588, "open"), ("C06", 906, "open"), ("C07", 374, "open"), ("C08", 324, "open"),
    ("C09", 503, "open"),
])
def test_collection_records(cid, remaining, status):
    rec = collection.record(_case("collection", cid))
    assert rec["remaining"] == remaining and rec["status"] == status


def test_partial_payment_split_across_bills_is_one_payment():
    rec = collection.record(_case("collection", "C02"))
    assert rec["amount_paid"] == 300
    assert [p["ref"] for p in rec["payments"]] == ["PAY-74410"]


def test_hardship_plan_amount():
    rec = collection.record(_case("collection", "C06"))
    assert rec["plan_eligible"] and rec["plan_instalment_amount"] == 302


# -------------------------------------------------------- verification gate
def test_collection_tools_refuse_before_verification():
    c, st = _case("collection", "C01"), collection.new_state()
    assert collection.get_balance(st, c)["error"] == "not_verified"
    assert collection.send_payment_link(st, c, mobile_last4="1441")["error"] == "not_verified"
    assert collection.record_promise(st, c, promise_date="2099-01-01")["error"] == "not_verified"
    # A callback / wrong-person note discloses nothing, so it works unverified.
    assert collection.log_case_note(st, c, category="callback", text="after 8")["ok"]


def test_verify_identity_and_wrong_person():
    c, st = _case("collection", "C07"), collection.new_state()
    out = collection.verify_identity(st, c, date_of_birth="1980-07-07", speaking_with="Sana, his wife")
    assert out["error"] == "not_the_customer" and not st["verified"]
    assert collection.verify_identity(st, c, id_last4="0000")["error"] == "details_do_not_match"
    assert "next" not in collection.verify_identity(st, c, id_last4="1111")   # 3 attempts, like support
    third = collection.verify_identity(st, c, id_last4="2222")
    assert third.get("next") == "offer_callback"
    assert collection.verify_identity(st, c, id_last4="9981")["error"] == "too_many_attempts"


def test_balance_after_verification():
    c, st = _case("collection", "C01"), collection.new_state()
    assert collection.verify_identity(st, c, date_of_birth="08/08/1984")["ok"]
    bal = collection.get_balance(st, c)
    assert bal["remaining"] == "723 dirhams"
    assert bal["months"] == ["August 2026", "September 2026"]
    assert bal["registered_mobile_last4"] == "1441"
    assert bal["case_reference"] == collection.case_reference(c)


# ---------------------------------------------------------------- yes-gate
def _verified_collection(cid="C01"):
    c, st = _case("collection", cid), collection.new_state()
    cust = data.customer_for_case(c)
    assert collection.verify_identity(st, c, id_last4=cust["id_last4"])["ok"]
    return c, st


def test_link_refused_until_the_send_question_was_asked():
    c, st = _verified_collection()
    out = collection.send_payment_link(st, c, mobile_last4="1441")
    assert out["error"] == "not_confirmed" and "NOTHING WAS SENT" in out["detail"]
    common.note_assistant_turn(st, "Shall I send the link to the mobile ending 1441?", "1441")
    common.note_customer_turn(st, "Yes please")
    sent = collection.send_payment_link(st, c, mobile_last4="1441")
    assert sent["ok"] and sent["amount"] == "723 dirhams"
    again = collection.send_payment_link(st, c, mobile_last4="1441")
    assert again["error"] == "already_sent"


def test_bare_number_or_no_is_not_a_yes():
    c, st = _verified_collection()
    common.note_assistant_turn(st, "Can I send it to the number ending 1441?", "1441")
    common.note_customer_turn(st, "1441")
    assert collection.send_payment_link(st, c, mobile_last4="1441")["error"] == "not_confirmed"
    common.note_customer_turn(st, "no, not now")
    assert collection.send_payment_link(st, c, mobile_last4="1441")["error"] == "not_confirmed"


def test_send_question_in_arabic_with_arabic_digits():
    st = {}
    common.note_assistant_turn(st, "أرسل لك الرابط على الرقم المنتهي بـ ١٤٤١؟", "1441")
    assert st["asked_send"] is True
    common.note_assistant_turn(st, "Your balance is 723 dirhams.", "1441")
    assert st["asked_send"] is False


def test_link_only_to_registered_mobile_and_not_under_review():
    c, st = _verified_collection()
    common.note_assistant_turn(st, "Send it to the mobile ending 1441?", "1441")
    assert collection.send_payment_link(st, c, mobile_last4="9999")["error"] == "mobile_mismatch"
    collection.escalate_for_review(st, c, reason="dispute", detail="roaming")
    assert collection.send_payment_link(st, c, mobile_last4="1441")["error"] == "under_review"


def test_nothing_due_when_paid():
    c, st = _verified_collection("C03")
    assert collection.send_payment_link(st, c, mobile_last4="1707")["error"] == "nothing_due"
    assert collection.payment_status(st, c)["state"] == "confirmed"


def test_unmatched_payment_claim():
    c, st = _verified_collection("C04")
    out = collection.payment_status(st, c, reference_given="PAY-11111", paid_on="2026-10-01")
    assert out["state"] == "not_found" and out["case_state"] == "open"
    esc = collection.escalate_for_review(st, c, reason="unmatched_payment")
    assert esc["case_reference"] == collection.case_reference(c)


def test_plan_quote_then_confirm_and_instalment_link():
    c, st = _verified_collection("C06")
    quote = collection.set_up_payment_plan(st, c, instalments=3, today=date(2026, 10, 7))
    assert quote["done"] is False and quote["terms"]["each"] == "302 riyals"
    assert collection.set_up_payment_plan(st, c, instalments=5)["error"] == "invalid_instalments"
    assert collection.set_up_payment_plan(st, c, instalments=3, confirm=True)["done"]
    common.note_assistant_turn(st, "Shall I send the first instalment link to the mobile ending 1328?", "1328")
    sent = collection.send_payment_link(st, c, mobile_last4="1328", instalment=True)
    assert sent["ok"] and sent["amount"] == "302 riyals"


def test_promise_window():
    c, st = _verified_collection("C09")
    late = collection.record_promise(st, c, promise_date="2026-10-17", today=date(2026, 10, 7))
    assert late["ok"] and late["within_policy"] is False
    assert collection.record_promise(st, c, promise_date="2026-10-01", today=date(2026, 10, 7))["error"] == "date_in_past"


# -------------------------------------------------------- case file / direct
def test_collection_case_file_contents_and_goal():
    paid = collection.case_file(_case("collection", "C03"), today=date(2026, 10, 7))
    assert "already PAID" in paid and "PAY-77812" in paid
    partial = collection.case_file(_case("collection", "C02"), today=date(2026, 10, 7))
    assert "300 riyals already paid and 303 riyals remaining" in partial
    hardship = collection.case_file(_case("collection", "C06"), today=date(2026, 10, 7))
    assert "3 monthly instalments of 302 riyals" in hardship
    for cid in ("C01", "C05", "C07"):
        text = collection.case_file(_case("collection", cid))
        assert "{" not in text and "}" not in text  # rendered into a format_map'd persona


def test_collection_direct_tools_are_gated_and_once_only():
    c, st = _case("collection", "C01"), collection.new_state()
    tools = {decl["name"]: fn for decl, fn in collection.direct_tools(c, st)}
    assert set(tools) == {"verify_identity", "send_payment_link", "set_up_payment_plan",
                          "record_promise_to_pay", "log_case_note", "escalate_for_review"}
    # Voice verifies in CODE too: nothing works until verify_identity matched,
    # and a wrong date of birth / ID is refused (the model never sees them).
    assert tools["send_payment_link"]({"mobile_last4": "1441"})["error"] == "not_verified"
    cust = data.customer_for_case(c)
    assert tools["verify_identity"]({"date_of_birth": "1990-01-01"})["error"] == "details_do_not_match"
    assert tools["verify_identity"]({"id_last4": cust["id_last4"]})["verified"] is True
    assert tools["send_payment_link"]({"mobile_last4": "1441"})["error"] == "not_confirmed"
    common.note_assistant_turn(st, "I can send a payment link to the mobile ending 1441 — shall I?", "1441")
    assert tools["send_payment_link"]({"mobile_last4": "1441"})["ok"]
    assert tools["send_payment_link"]({"mobile_last4": "1441"})["error"] == "already_sent"


# ------------------------------------------------------------------- sales
def _verified_sales(cid):
    c, st = _case("sales", cid), sales.new_state()
    cust = data.customer_for_case(c)
    assert sales.verify_identity(st, c, id_last4=cust["id_last4"])["ok"]
    return c, st


def test_sales_profile_needs_verification_catalog_does_not():
    c, st = _case("sales", "O09"), sales.new_state()
    assert sales.customer_profile(st, c)["error"] == "not_verified"
    cat = sales.catalog_lookup(c, product_id="home_fiber_1g")
    assert cat["price"] == "429 riyals a month on a 12-month contract, or 409 riyals a month on a 24-month contract"
    assert sales.catalog_lookup(c, product_id="tv max")["error"] == "unknown_product"


def test_sales_offer_link_gate_and_already_has():
    c, st = _verified_sales("O09")
    assert sales.send_offer_link(st, c, product_id="home_fiber_1g", mobile_last4="3256")["error"] == "not_confirmed"
    assert sales.send_offer_link(st, c, product_id="home_fiber_300", mobile_last4="3256")["error"] == "already_has"
    common.note_assistant_turn(st, "Shall I text the details to the number ending 3256?", "3256")
    assert sales.send_offer_link(st, c, product_id="home_fiber_1g", mobile_last4="3256")["ok"]
    assert sales.send_offer_link(st, c, product_id="home_fiber_1g", mobile_last4="3256")["error"] == "already_sent"


def test_sales_not_interested_twice_and_do_not_call():
    c, st = _case("sales", "O05"), sales.new_state()
    assert sales.log_outcome(st, c, outcome="not_interested")["next"] == "one_value_line"
    assert sales.log_outcome(st, c, outcome="not_interested")["next"] == "end_call"
    c7, st7 = _verified_sales("O07")
    assert sales.log_outcome(st7, c7, outcome="do_not_call")["next"] == "end_call"
    common.note_assistant_turn(st7, "Send it to the number ending 0452?", "0452")
    assert sales.send_offer_link(st7, c7, product_id="mesh_wifi_addon", mobile_last4="0452")["error"] == "do_not_call"
    assert sales.book_followup(st7, c7, followup_date="2026-10-10")["error"] == "do_not_call"


def test_sales_followup_window():
    c, st = _case("sales", "O06"), sales.new_state()
    today = date(2026, 10, 7)
    assert sales.book_followup(st, c, followup_date="2026-11-30", today=today)["error"] == "too_far"
    ok = sales.book_followup(st, c, followup_date="2026-10-13", followup_time="16:00", today=today)
    assert ok["ok"] and ok["weekday"] == "Tuesday"


def test_sales_case_file_has_the_offer_and_no_discount():
    text = sales.case_file(_case("sales", "O02"), today=date(2026, 10, 7))
    assert "THE OFFER for this call: Family line add-on" in text
    assert "3 lines = 147 dirhams a month" in text
    assert "NO discount" in text and "{" not in text
    biz = sales.case_file(_case("sales", "O03"))
    assert "Business Fiber 500" in biz


def test_sales_direct_tools():
    c, st = _case("sales", "O01"), sales.new_state()
    tools = {decl["name"]: fn for decl, fn in sales.direct_tools(c, st)}
    assert set(tools) == {"verify_identity", "send_offer_link", "book_followup", "log_outcome"}
    assert not st["verified"]  # verified only by the tool, never pre-marked
    cust = data.customer_for_case(c)
    assert tools["send_offer_link"]({"product_id": c["offer"], "mobile_last4": cust["mobile"][-4:]})["error"] \
        == "not_verified"
    assert tools["verify_identity"]({"id_last4": "0000"})["error"] == "details_do_not_match"
    assert tools["verify_identity"]({"date_of_birth": cust["dob"]})["verified"] is True
    assert st["verified"] is True


def test_outbound_case_files_never_carry_the_identity_answers():
    for module, key in ((collection, "collection"), (sales, "sales")):
        for meta in data.cases(key):
            c = _case(key, meta["id"])
            cust = data.customer_for_case(c)
            text = module.case_file(c)
            assert cust["dob"] not in text and data.say_date(cust["dob"]) not in text, meta["id"]
            assert f"ID last 4 digits {cust['id_last4']}" not in text and "verify_identity" in text
