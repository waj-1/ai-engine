"""The demo agents' identity check forgives the FIELD, never the value.

Live 2026-10-14 (collection C01, DOB 1984-08-08, ID last 4 1450): the caller
said «ثمانية ثمانية أربعة وثمانين» (8/8/84); the realtime model filed it as
id_last4="8884" and it failed. The full date then arrived correctly — and
was refused as too_many_attempts, the agent telling the caller «البيانات ما
تتطابق» about the right answer.
"""
import pytest

from runtime.tools.agents_demo import collection, common, data, sales, support


def _case(agent="collection", cid="C01"):
    return data.resolve_case(agent, cid)


@pytest.mark.parametrize("dob,id4", [
    (None, "8884"),                 # the live failure: a short date in the ID field
    ("8/8/84", None), ("08-08-84", None), ("1984-08-08", None), ("08/08/1984", None),
    ("٨/٨/١٩٨٤", None), ("19840808", None), (None, "1450"), ("1450", None),
])
def test_the_right_value_verifies_whatever_field_it_arrives_in(dob, id4):
    cust = data.customer_for_case(_case())
    assert cust["dob"] == "1984-08-08" and cust["id_last4"] == "1450"
    assert common.second_factor_matches(cust, dob, id4)


@pytest.mark.parametrize("dob,id4", [
    ("1007-07-07", None), (None, "8885"), ("9/8/84", None), (None, "1451"), ("84", None),
])
def test_a_wrong_value_still_fails(dob, id4):
    assert not common.second_factor_matches(data.customer_for_case(_case()), dob, id4)


def test_the_live_call_now_verifies_on_the_second_attempt():
    st, case = collection.new_state(), _case()
    assert collection.verify_identity(st, case, date_of_birth="1007-07-07")["error"] == "details_do_not_match"
    assert collection.verify_identity(st, case, id_last4="8884")["verified"] is True


def test_three_attempts_and_a_repeat_is_not_a_new_guess():
    st, case = collection.new_state(), _case()
    collection.verify_identity(st, case, date_of_birth="1990-01-01")
    collection.verify_identity(st, case, date_of_birth="01/01/1990")   # same date, re-heard
    assert st["id_failures"] == 1
    collection.verify_identity(st, case, id_last4="0000")
    out = collection.verify_identity(st, case, id_last4="9999")
    assert out.get("next") == "offer_callback" and st["id_failures"] == 3


def test_the_lockout_never_tells_the_caller_the_details_are_wrong():
    st, case = collection.new_state(), _case()
    for v in ("0001", "0002", "0003"):
        collection.verify_identity(st, case, id_last4=v)
    out = collection.verify_identity(st, case, date_of_birth="1984-08-08")
    assert out["error"] == "too_many_attempts"
    assert "Do NOT say the details are wrong" in out["detail"]


def test_sales_and_support_share_the_matcher():
    st = sales.new_state()
    scase = data.resolve_case("sales", None)
    cust = data.customer_for_case(scase)
    d = cust["dob"]
    short = f"{int(d[8:])}{int(d[5:7])}{d[2:4]}"
    assert sales.verify_identity(st, scase, id_last4=short)["verified"] is True
    sst = support.new_state()
    acct = next(iter(data.customers()))
    c = data.customers()[acct]
    out = support.verify_customer(sst, account_number=acct, id_last4=f"{int(c['dob'][8:])}{int(c['dob'][5:7])}{c['dob'][2:4]}")
    assert out.get("verified") is True or out.get("ok") is True
