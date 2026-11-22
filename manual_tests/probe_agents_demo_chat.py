"""Drive scripted TEXT conversations against the AI-agents demo portal.

Not part of the pytest suite — it talks to a RUNNING engine and real models.
For each scripted conversation it posts the caller's lines one by one to
POST /agents/chat/stream, carries the signed state token between turns, and
prints every reply with its time-to-first-token, total time and the tools
that fired.

    python manual_tests/probe_agents_demo_chat.py --url http://127.0.0.1:8077
    python manual_tests/probe_agents_demo_chat.py --only collection --json out.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

import httpx

SCRIPTS: Dict[str, Dict[str, Any]] = {
    "support-S01": {"agent": "support", "locale": "en", "case": "S01", "lines": [
        "Hi, my Wi-Fi is crawling in the living room and the match is on!",
        "Account number 41203317, date of birth 14 March 1991.",
        "OK, what should I do?",
    ]},
    "support-S03-verify-first": {"agent": "support", "locale": "en", "case": "S03", "lines": [
        "Why is my September bill so high? What's on it?",
        "My account is 71304412 and my date of birth is 21 July 1990.",
        "Sorry — 21 July 1993.",
        "Do I get a refund for that?",
    ]},
    "support-S04-ar": {"agent": "support", "locale": "ar", "case": "S04", "lines": [
        "السلام عليكم، انخصم مني مبلغ الفاتورة مرتين",
        "رقم الحساب 41209908 وآخر 4 أرقام من الهوية 2290",
        "رجعها على البطاقة لو سمحت",
    ]},
    "support-S06": {"agent": "support", "locale": "en", "case": "S06", "lines": [
        "My internet is dead and there's a red LOS light on the fiber box.",
        "Account 71308821, ID ending 4459.",
        "The first slot works for me.",
    ]},
    "inquiries-I04": {"agent": "inquiries", "locale": "en", "case": "I04", "lines": [
        "I own a café in Al Rawdah, Jeddah with 6 staff. What do you have for internet and phones?",
        "How much is it and is there a backup if the fiber drops?",
        "Do you have a discount for small businesses?",
    ]},
    "inquiries-I02-ar": {"agent": "inquiries", "locale": "ar", "case": "I02", "lines": [
        "مرحبا، بنسكن فيلا جديدة في النرجس بالرياض يوم الخميس. عندكم فايبر هناك؟",
        "طيب كم سعر الـ 5G وكم ياخذ وقت؟",
    ]},
    "inquiries-I07": {"agent": "inquiries", "locale": "en", "case": "I07", "lines": [
        "Hi, I manage a dental clinic in Al Ulaya, Al Khobar. Do you have business fiber there?",
        "Please have sales call me. I'm Lama Al Otaibi, mobile 0596667788.",
        "Sunday at 10 am works.",
    ]},
    "collection-C01": {"agent": "collection", "locale": "en", "case": "C01", "lines": [
        "Hello?",
        "Yes, this is Yousef. My date of birth is 8 August 1984.",
        "OK, fine — send it.",
        "That's all, thanks.",
    ]},
    "collection-C07": {"agent": "collection", "locale": "en", "case": "C07", "lines": [
        "Hello? This is Sana, Bilal's wife — he's not home.",
        "What is it about? Is it the bill?",
        "He's back after 8 pm.",
    ]},
    "sales-O09": {"agent": "sales", "locale": "en", "case": "O09", "lines": [
        "Hello?",
        "Sure, I have a minute. My ID ends 5594.",
        "Honestly the kids complain about lag all the time.",
        "How much is it? Any discount?",
        "OK send me the details.",
    ]},
}


def run_turn(client: httpx.Client, url: str, body: Dict[str, Any]) -> Dict[str, Any]:
    started = time.monotonic()
    first: Optional[float] = None
    answer, tools, thoughts, token, error = "", [], [], None, None
    with client.stream("POST", f"{url}/agents/chat/stream", json=body, timeout=120) as resp:
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}: {resp.read().decode()[:200]}"}
        for line in resp.iter_lines():
            if not line.startswith("data: "):
                continue
            evt = json.loads(line[6:])
            t = evt.get("type")
            if t == "answer":
                if first is None:
                    first = time.monotonic() - started
                answer += evt.get("content") or ""
            elif t == "tool_activity":
                tools.extend(evt.get("tools") or [])
            elif t == "thought":
                thoughts.append(evt.get("content"))
            elif t == "state":
                token = evt.get("token")
            elif t == "error":
                error = evt.get("message")
    return {"answer": answer.strip(), "ttft_s": round(first or 0, 2),
            "total_s": round(time.monotonic() - started, 2), "tools": tools, "state": token, "error": error}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--only", help="substring filter on script names")
    ap.add_argument("--json", help="write the transcript here")
    args = ap.parse_args()
    out: List[Dict[str, Any]] = []
    with httpx.Client() as client:
        for name, script in SCRIPTS.items():
            if args.only and args.only not in name:
                continue
            session, state = str(uuid.uuid4()), None
            print(f"\n=== {name} ===")
            for line in script["lines"]:
                body = {"agent": script["agent"], "message": line, "session_id": session,
                        "locale": script["locale"], "case_id": script.get("case"), "state": state}
                res = run_turn(client, args.url, body)
                state = res.get("state") or state
                print(f"> {line}\n< [{res.get('ttft_s')}s/{res.get('total_s')}s tools={res.get('tools')}] "
                      f"{res.get('answer') or res.get('error')}")
                out.append({"script": name, "caller": line, **{k: v for k, v in res.items() if k != "state"}})
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
