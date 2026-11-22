"""Business-inquiry tools: prices only from the catalog, coverage only from
the area table, leads/callbacks validated."""
from __future__ import annotations

from datetime import date

from runtime.tools.agents_demo import inquiries


def test_list_plans_prices_per_country():
    ae = inquiries.list_plans(category="home", country="Dubai")
    fiber_1g = next(p for p in ae["plans"] if p["id"] == "home_fiber_1g")
    assert fiber_1g["monthly_price"] == {"AE": "449 dirhams"}
    assert fiber_1g["price_on_24_month_contract"] == {"AE": "429 dirhams"}
    sa = inquiries.list_plans(category="business", country="Saudi Arabia")
    bundle = next(p for p in sa["plans"] if p["id"] == "sme_bundle")
    assert bundle["monthly_price"] == {"SA": "1,149 riyals"}
    assert any(p["id"] == "biz_backup_5g" for p in sa["plans"])  # add-ons listed with business


def test_unknown_country_lists_both_prices():
    out = inquiries.list_plans(category="mobile")
    assert set(out["plans"][0]["monthly_price"]) == {"AE", "SA"}


def test_not_offered_is_explicit():
    out = inquiries.list_plans(category="tv channels")
    assert out["ok"] is False and out["error"] == "not_offered"
    assert any("TV" in n for n in out["not_offered"])
    assert "no discounts" in inquiries.list_plans()["note"]


def test_coverage_from_the_area_table_only():
    reem = inquiries.check_coverage(area="Al Reem Island")
    assert reem["fiber"] and reem["fiber_installation"].startswith("2 to 5")
    narjis = inquiries.check_coverage(area="النرجس")
    assert narjis["fiber"] is False and narjis["fiber_planned"] == "second quarter of 2027"
    assert narjis["available_home_plans"] == ["Home 5G Wireless"]
    assert inquiries.check_coverage(area="Atlantis")["error"] == "unknown_area"


def test_compare_gives_the_exact_difference():
    out = inquiries.compare_plans(plan_ids=["home_fiber_500", "home_fiber_1g"], country="UAE")
    assert out["monthly_difference"] == "100 dirhams" and out["cheaper"] == "Home Fiber 500"
    assert inquiries.compare_plans(plan_ids=["home_fiber_500"])["error"] == "need_two_plans"
    assert inquiries.compare_plans(plan_ids=["home_fiber_500", "tv_max"])["error"] == "unknown_plan"


def test_lead_then_callback_within_window():
    st = inquiries.new_state()
    today = date(2026, 10, 7)
    assert inquiries.book_callback(st, callback_date="2026-10-09", today=today)["error"] == "no_lead"
    assert inquiries.capture_lead(st, name="Lama", mobile="12345", interest="fiber")["error"] == "invalid_mobile"
    lead = inquiries.capture_lead(st, name="Lama", mobile="+966 50 999 8877", interest="business fiber")
    assert lead["ok"] and lead["mobile_last4"] == "8877"
    assert inquiries.book_callback(st, callback_date="2026-11-30", today=today)["error"] == "too_far"
    assert inquiries.book_callback(st, callback_date="2026-10-01", today=today)["error"] == "date_in_past"
    cb = inquiries.book_callback(st, callback_date="2026-10-09", callback_time="16:00", today=today)
    assert cb["ok"] and cb["to_mobile_ending"] == "8877"
    assert inquiries.book_callback(st, callback_date="2026-10-10", today=today)["error"] == "already_booked"
