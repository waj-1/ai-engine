"""Unbacked booking-action claims are caught and replaced.

The replies below are the agent's own, from the prod inbox evals on Elegance
and the Messenger / Instagram test on 2026-10-17/19: each claimed a booking no
tool had made, or is a near miss that must be left alone.
"""
from __future__ import annotations

import json

import pytest

from common.action_claims import claimed_actions, guard_action_claims

_DONE = json.dumps({"booking_id": "b1", "state": "booked"})
_NOT_CONFIRMED = json.dumps({"error": "confirmation_required", "message": "Ask the customer to confirm."})
_FAILED = json.dumps({"error": "add_booking_online returned 409", "details": "slot taken"})


@pytest.mark.parametrize("reply, kind", [
    ("Your Classic Haircut is booked for tomorrow, October 18th, at 11:00.", "book"),
    ("Your Classic Haircut with Sara is all set for tomorrow.", "book"),
    ("I've set that up for you. Shall I go ahead and book this?", "book"),
    ("Great! We've reserved 11:00 tomorrow for you.", "book"),
    ("تم حجز تشذيب لحية يوم السبت 24 أكتوبر الساعة 12:00", "book"),
    ("تم، حجزت لك تشذيب لحية مع ديفيد جونسون", "book"),
    ("حجزنا لك الموعد بكرة الساعة ١٠", "book"),
    ("أبشر، حجزت لك قص الشعر بكرة الساعة ١٠", "book"),
    ("تم تأكيد حجزك لقص شعر الرجال عند ايس اليوم السبت الساعة 5:30 مساءً.", "book"),
    ("تم تثبيت موعدك بكرة الساعة ١٠", "book"),
    ("I've cancelled your Sunday appointment.", "cancel"),
    ("تم إلغاء موعدك", "cancel"),
    ("I've moved your booking to Friday at 16:00.", "move"),
    ("تم تغيير موعدك ليوم الجمعة", "move"),
    ("I've added a Beard Trim to your booking.", "add"),
    ("تم إضافة الخدمة", "add"),
])
def test_claims_are_recognised(reply, kind):
    assert kind in claimed_actions(reply)


@pytest.mark.parametrize("reply", [
    "Shall I go ahead and book this for you?",
    "أحجز لك الموعد الساعة ١٠؟",
    "تم، السبت الساعة 12:00 متاح لخدمة لحية. ممكن الاسم؟",
    "We have availability tomorrow. Which time works best?",
    "We look forward to seeing you!",
    "Would you like me to cancel this appointment for you?",
    "I couldn't find any upcoming appointments. Your recent bookings are already marked as cancelled.",
])
def test_questions_and_plain_answers_are_not_claims(reply):
    assert claimed_actions(reply) == set()


def test_a_booking_that_was_looked_up_is_not_a_claim():
    # Describing an existing booking in the cancel flow is not claiming one.
    reply = "I see your Classic Haircut is booked for Sunday at 11:00. Shall I cancel it?"
    assert guard_action_claims(reply, [("list_customer_bookings", "{}")]) == (reply, None)
    assert guard_action_claims(reply, [])[1] == "unbacked:book"


def test_a_claim_the_tool_backs_is_kept():
    reply = "Your Classic Haircut is booked for tomorrow at 11:00."
    assert guard_action_claims(reply, [("submit_booking", _DONE)]) == (reply, None)


@pytest.mark.parametrize("reply, results, expected", [
    # no tool at all
    ("Your Classic Haircut is booked for tomorrow.", [],
     "Just to confirm before I go ahead: shall I book this for you?"),
    # the tool refused for want of a confirmation: not done yet, so ask
    ("تم حجز تشذيب لحية", [("submit_booking", _NOT_CONFIRMED)],
     "للتأكيد قبل ما أكمل: أحجز لك الموعد؟"),
    # a different action ran: it doesn't back this one
    ("I've moved your booking to Friday.", [("cancel_booking", json.dumps({"success": True}))],
     "Just to confirm before I go ahead: shall I move your booking to the new time?"),
])
def test_an_unbacked_claim_becomes_a_question(reply, results, expected):
    out, reason = guard_action_claims(reply, results)
    assert out == expected and reason.startswith("unbacked:")


@pytest.mark.parametrize("reply, lang_word", [
    ("I've cancelled it for you.", "Sorry"),
    ("تم إلغاء موعدك", "عذراً"),
])
def test_a_claim_after_a_failed_tool_hands_off(reply, lang_word):
    out, reason = guard_action_claims(reply, [("cancel_booking", json.dumps({"error": "boom"}))])
    assert out.startswith(lang_word) and reason == "failed:cancel"


def test_a_retry_that_succeeded_backs_the_claim():
    reply = "We've reserved 11:00 for you."
    assert guard_action_claims(reply, [("submit_booking", _FAILED), ("submit_booking", _DONE)]) == (reply, None)


def test_a_passive_claim_after_a_refused_change_is_caught():
    # Live test 2026-10-22: "sure" was not read as a yes, reschedule_booking
    # refused, and in a turn that had also read the bookings "has been moved
    # to Sara" went out, while the booking stayed with Waed.
    results = [
        ("list_customer_bookings", json.dumps({"bookings": [{"booking_id": "b1"}]})),
        ("reschedule_booking", json.dumps({"error": "confirmation_required"})),
    ]
    reply = "Your appointment for a Classic Haircut tomorrow has been moved to Sara at 12:00."
    new, reason = guard_action_claims(reply, results)
    assert reason == "unbacked:move"
    assert "shall I move your booking" in new


def test_updated_and_is_now_with_after_a_failed_move_hand_off():
    # Live test 2026-10-22: edit_booking_online 400'd, and "I've updated your
    # appointment ... is now with Sara" went out; the booking stayed with Waed.
    results = [
        ("list_customer_bookings", json.dumps({"bookings": [{"booking_id": "b1"}]})),
        ("reschedule_booking", json.dumps({"error": "edit_booking_online returned 400"})),
    ]
    reply = ("I've updated your appointment, Muq. Your Signature Haircut & Blowdry for "
             "tomorrow at 14:00 is now with Sara.")
    new, reason = guard_action_claims(reply, results)
    assert reason == "failed:move"
    assert "couldn't complete that" in new
    assert guard_action_claims("Your booking is now with Sara.", results)[1] == "failed:move"
