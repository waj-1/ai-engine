#!/usr/bin/env python3
"""
Comprehensive multilingual testing for WAJ AI Agent
Tests agent behavior across different languages and scenarios
"""
import asyncio
import sys
import argparse
from typing import List, Dict, Any
import json
import aiohttp
from datetime import datetime
import uuid

# Test scenarios organized by language
TEST_SCENARIOS = {
    "en": {
        "name": "English",
        "tests": [
            {
                "name": "Sales Today",
                "message": "Show my sales today",
                "expected_tool": "get_business_performance_wrapper",
                "check_response": ["sales", "revenue", "today", "AED"]
            },
            {
                "name": "Top Services",
                "message": "What are my most popular services this week?",
                "expected_tool": "get_services_categories_wrapper",
                "check_response": ["service", "popular", "week"]
            },
            {
                "name": "New Clients",
                "message": "How many new clients did we get?",
                "expected_tool": "get_clients_wrapper",
                "check_response": ["client", "new"]
            },
            {
                "name": "Documentation",
                "message": "How do I add a new service?",
                "expected_tool": "search_documentation_wrapper",
                "check_response": ["click", "service", "add"]
            },
            {
                "name": "Follow-up Question",
                "messages": [
                    {"role": "user", "content": "Show my revenue this week"},
                    {"role": "assistant", "content": "This week's revenue is AED 419,576"},
                ],
                "message": "Why is it higher than last week?",
                "check_response": ["week", "revenue", "higher", "because"]
            }
        ]
    },
    "ar": {
        "name": "Arabic",
        "tests": [
            {
                "name": "Sales Today (Arabic)",
                "message": "أظهر مبيعاتي اليوم",
                "expected_tool": "get_business_performance_wrapper",
                "check_response": ["مبيعات", "إيرادات", "اليوم", "درهم"]
            },
            {
                "name": "Top Services (Arabic)",
                "message": "ما هي أكثر الخدمات طلبًا هذا الشهر؟",
                "expected_tool": "get_services_categories_wrapper",
                "check_response": ["خدمات", "الأكثر", "الشهر"]
            },
            {
                "name": "New Clients (Arabic)",
                "message": "كم عدد العملاء الجدد؟",
                "expected_tool": "get_clients_wrapper",
                "check_response": ["عملاء", "جدد"]
            }
        ]
    },
    "tr": {
        "name": "Turkish",
        "tests": [
            {
                "name": "Sales Today (Turkish)",
                "message": "Bugünkü satışlarımı göster",
                "expected_tool": "get_business_performance_wrapper",
                "check_response": ["satış", "gelir", "bugün"]
            }
        ]
    }
}


class AgentTester:
    """Test runner for WAJ AI Agent"""
    
    def __init__(self, base_url: str, token: str, business_id: str):
        self.base_url = base_url
        self.token = token
        self.business_id = business_id
        self.branch_id = "3b6e1db9-63de-4491-976c-442f9d9a653a"  # Default branch
        self.results = []
    
    async def run_test(self, locale: str, test: Dict[str, Any]) -> Dict[str, Any]:
        """Run a single test case"""
        test_name = test["name"]
        print(f"\n{'='*60}")
        print(f"TEST: {test_name}")
        print(f"{'='*60}")
        
        # Prepare request
        previous_messages = test.get("messages", [])
        is_first = len(previous_messages) == 0
        
        payload = {
            "message": test["message"],
            "session_id": str(uuid.uuid4()),  # Generate proper UUID
            "business_id": self.business_id,
            "branch_id": self.branch_id,
            "locale": locale,
            "currency": "AED",
            "is_first_message": is_first,
            "ai_access": {"all": True, "data": True, "docs": True, "actions": True},
            "previous_messages": previous_messages
        }
        
        print(f"Message: {test['message']}")
        if previous_messages:
            print(f"Previous messages: {len(previous_messages)}")
        
        result = {
            "test_name": test_name,
            "locale": locale,
            "message": test["message"],
            "passed": False,
            "events": [],
            "errors": [],
            "metrics": {}
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base_url}/chat/stream",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream"
                    },
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        result["errors"].append(f"HTTP {response.status}: {error_text}")
                        print(f"❌ HTTP Error: {response.status}")
                        return result
                    
                    # Parse SSE events
                    events_by_type = {
                        "thought": [],
                        "observation": [],
                        "answer": [],
                        "suggestions": None,
                        "usage": None
                    }
                    
                    async for line in response.content:
                        line_str = line.decode('utf-8').strip()
                        
                        if line_str.startswith('data: '):
                            event_data = line_str[6:]  # Remove 'data: ' prefix
                            try:
                                event = json.loads(event_data)
                                event_type = event.get("type")
                                result["events"].append(event)
                                
                                if event_type in events_by_type:
                                    if event_type in ["thought", "observation", "answer"]:
                                        content = event.get("content", "")
                                        events_by_type[event_type].append(content)
                                        print(f"  [{event_type.upper()}] {content[:100]}")
                                    elif event_type == "suggestions":
                                        events_by_type["suggestions"] = event.get("questions", [])
                                        print(f"  [SUGGESTIONS] {len(event.get('questions', []))} questions")
                                    elif event_type == "usage":
                                        events_by_type["usage"] = event
                                        print(f"  [USAGE] {event.get('total_tokens')} tokens ({event.get('model')})")
                                    elif event_type == "title":
                                        print(f"  [TITLE] {event.get('content')}")
                                
                            except json.JSONDecodeError as e:
                                result["errors"].append(f"JSON decode error: {e}")
                    
                    # Analyze results
                    result["metrics"] = {
                        "thought_count": len(events_by_type["thought"]),
                        "observation_count": len(events_by_type["observation"]),
                        "answer_chunks": len(events_by_type["answer"]),
                        "total_tokens": events_by_type["usage"].get("total_tokens", 0) if events_by_type["usage"] else 0,
                        "model": events_by_type["usage"].get("model", "unknown") if events_by_type["usage"] else "unknown"
                    }
                    
                    # Check if test passed
                    passed_checks = []
                    
                    # Check 1: Got answer chunks
                    if len(events_by_type["answer"]) > 0:
                        passed_checks.append("✓ Answer chunks received")
                    else:
                        result["errors"].append("No answer chunks received")
                    
                    # Check 2: Correct language in response
                    full_answer = " ".join(events_by_type["answer"])
                    if test.get("check_response"):
                        # For non-English, check if response contains expected keywords
                        found_keywords = [kw for kw in test["check_response"] if kw.lower() in full_answer.lower()]
                        if found_keywords:
                            passed_checks.append(f"✓ Response contains expected keywords: {found_keywords}")
                        else:
                            result["errors"].append(f"Response missing expected keywords: {test['check_response']}")
                    
                    # Check 3: Reasonable token usage (< 100k)
                    if result["metrics"]["total_tokens"] < 100000:
                        passed_checks.append(f"✓ Reasonable token usage: {result['metrics']['total_tokens']}")
                    else:
                        result["errors"].append(f"Token usage too high: {result['metrics']['total_tokens']}")
                    
                    # Check 4: Got thoughts and observations
                    if len(events_by_type["thought"]) > 0:
                        passed_checks.append(f"✓ Thoughts: {len(events_by_type['thought'])}")
                    
                    # Overall pass/fail
                    result["passed"] = len(result["errors"]) == 0 and len(events_by_type["answer"]) > 0
                    
                    print(f"\n{'='*60}")
                    if result["passed"]:
                        print(f"✅ PASSED")
                        for check in passed_checks:
                            print(f"  {check}")
                    else:
                        print(f"❌ FAILED")
                        for error in result["errors"]:
                            print(f"  ❌ {error}")
                        for check in passed_checks:
                            print(f"  {check}")
                    
                    print(f"\nMetrics:")
                    print(f"  Thoughts: {result['metrics']['thought_count']}")
                    print(f"  Observations: {result['metrics']['observation_count']}")
                    print(f"  Answer chunks: {result['metrics']['answer_chunks']}")
                    print(f"  Total tokens: {result['metrics']['total_tokens']}")
                    print(f"  Model: {result['metrics']['model']}")
                    
        except Exception as e:
            result["errors"].append(f"Exception: {str(e)}")
            print(f"❌ EXCEPTION: {e}")
            import traceback
            traceback.print_exc()
        
        return result
    
    async def run_all_tests(self, languages: List[str] = None):
        """Run all test scenarios"""
        if languages is None:
            languages = list(TEST_SCENARIOS.keys())
        
        print("\n" + "="*60)
        print("WAJ AI AGENT - MULTILINGUAL TEST SUITE")
        print("="*60)
        print(f"Base URL: {self.base_url}")
        print(f"Business ID: {self.business_id}")
        print(f"Languages: {', '.join(languages)}")
        print(f"Time: {datetime.now().isoformat()}")
        
        all_results = []
        
        for locale in languages:
            if locale not in TEST_SCENARIOS:
                print(f"\n⚠️  Warning: No tests defined for locale '{locale}'")
                continue
            
            scenario = TEST_SCENARIOS[locale]
            print(f"\n\n{'#'*60}")
            print(f"# Testing {scenario['name']} ({locale})")
            print(f"{'#'*60}")
            
            for test in scenario["tests"]:
                result = await self.run_test(locale, test)
                all_results.append(result)
                self.results.append(result)
                
                # Small delay between tests
                await asyncio.sleep(2)
        
        # Print summary
        self.print_summary()
        
        return all_results
    
    def print_summary(self):
        """Print test summary"""
        print("\n\n" + "="*60)
        print("TEST SUMMARY")
        print("="*60)
        
        total = len(self.results)
        passed = sum(1 for r in self.results if r["passed"])
        failed = total - passed
        
        print(f"\nTotal Tests: {total}")
        print(f"Passed: {passed} ✅")
        print(f"Failed: {failed} ❌")
        print(f"Pass Rate: {(passed/total*100) if total > 0 else 0:.1f}%")
        
        # By language
        print(f"\n{'Locale':<10} {'Tests':<8} {'Passed':<8} {'Failed':<8}")
        print("-" * 40)
        for locale in set(r["locale"] for r in self.results):
            locale_results = [r for r in self.results if r["locale"] == locale]
            locale_passed = sum(1 for r in locale_results if r["passed"])
            locale_failed = len(locale_results) - locale_passed
            print(f"{locale:<10} {len(locale_results):<8} {locale_passed:<8} {locale_failed:<8}")
        
        # Failed tests details
        if failed > 0:
            print(f"\n{'='*60}")
            print("FAILED TESTS DETAILS")
            print("="*60)
            for result in self.results:
                if not result["passed"]:
                    print(f"\n❌ {result['test_name']} ({result['locale']})")
                    print(f"   Message: {result['message']}")
                    for error in result["errors"]:
                        print(f"   Error: {error}")
        
        # Token usage stats
        print(f"\n{'='*60}")
        print("TOKEN USAGE STATISTICS")
        print("="*60)
        
        total_tokens = sum(r["metrics"].get("total_tokens", 0) for r in self.results)
        avg_tokens = total_tokens / len(self.results) if self.results else 0
        max_tokens = max((r["metrics"].get("total_tokens", 0) for r in self.results), default=0)
        min_tokens = min((r["metrics"].get("total_tokens", 0) for r in self.results), default=0)
        
        print(f"Total tokens used: {total_tokens:,}")
        print(f"Average per test: {avg_tokens:,.0f}")
        print(f"Max: {max_tokens:,}")
        print(f"Min: {min_tokens:,}")
        
        # Answer chunk stats
        total_chunks = sum(r["metrics"].get("answer_chunks", 0) for r in self.results)
        avg_chunks = total_chunks / len(self.results) if self.results else 0
        
        print(f"\nAnswer chunks:")
        print(f"  Total: {total_chunks}")
        print(f"  Average per test: {avg_chunks:.1f}")
        
        print(f"\n{'='*60}\n")


async def main():
    """Main test runner"""
    parser = argparse.ArgumentParser(description="Test WAJ AI Agent multilingual capabilities")
    parser.add_argument("--url", default="http://localhost:8000", help="Base URL of the agent API")
    parser.add_argument("--token", required=True, help="User JWT token")
    parser.add_argument("--business-id", required=True, help="Business ID")
    parser.add_argument("--languages", nargs="+", default=["en", "ar"], help="Languages to test")
    
    args = parser.parse_args()
    
    tester = AgentTester(args.url, args.token, args.business_id)
    await tester.run_all_tests(args.languages)
    
    # Exit code based on results
    failed_count = sum(1 for r in tester.results if not r["passed"])
    sys.exit(0 if failed_count == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())

