#!/usr/bin/env python3
"""
Deep conversation test - follows suggested questions to test multi-turn conversations
and identify where token explosion occurs
"""
import asyncio
import sys
import argparse
import json
import aiohttp
from datetime import datetime
import uuid


class DeepConversationTester:
    """Test multi-turn conversations"""
    
    def __init__(self, base_url: str, token: str, business_id: str):
        self.base_url = base_url
        self.token = token
        self.business_id = business_id
        self.branch_id = "3b6e1db9-63de-4491-976c-442f9d9a653a"
        self.session_id = str(uuid.uuid4())
        self.conversation_history = []
        self.token_usage_history = []
    
    async def send_message(self, message: str, locale: str = "en") -> dict:
        """Send a message and collect response"""
        print(f"\n{'='*80}")
        print(f"USER ({locale}): {message}")
        print(f"{'='*80}")
        
        payload = {
            "message": message,
            "session_id": self.session_id,
            "business_id": self.business_id,
            "branch_id": self.branch_id,
            "locale": locale,
            "currency": "AED",
            "is_first_message": len(self.conversation_history) == 0,
            "ai_access": {"all": True, "data": True, "docs": True, "actions": True},
            "previous_messages": self.conversation_history.copy()
        }
        
        result = {
            "user_message": message,
            "thoughts": [],
            "observations": [],
            "answer_chunks": [],
            "full_answer": "",
            "suggestions": [],
            "usage": None,
            "errors": []
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
                        print(f"❌ HTTP {response.status}: {error_text}")
                        result["errors"].append(f"HTTP {response.status}")
                        return result
                    
                    async for line in response.content:
                        line_str = line.decode('utf-8').strip()
                        
                        if line_str.startswith('data: '):
                            event_data = line_str[6:]
                            try:
                                event = json.loads(event_data)
                                event_type = event.get("type")
                                
                                if event_type == "thought":
                                    content = event.get("content", "")
                                    result["thoughts"].append(content)
                                    print(f"💭 THOUGHT: {content}")
                                
                                elif event_type == "observation":
                                    content = event.get("content", "")
                                    result["observations"].append(content)
                                    print(f"👁️  OBSERVATION: {content}")
                                
                                elif event_type == "answer":
                                    chunk = event.get("content", "")
                                    result["answer_chunks"].append(chunk)
                                    result["full_answer"] += chunk
                                    print(chunk, end="", flush=True)
                                
                                elif event_type == "suggestions":
                                    result["suggestions"] = event.get("questions", [])
                                    print(f"\n\n💡 SUGGESTIONS:")
                                    for i, q in enumerate(result["suggestions"], 1):
                                        print(f"  {i}. {q}")
                                
                                elif event_type == "usage":
                                    result["usage"] = event
                                    print(f"\n📊 USAGE:")
                                    print(f"  Model: {event.get('model')}")
                                    print(f"  Input tokens: {event.get('input_tokens'):,}")
                                    print(f"  Output tokens: {event.get('output_tokens'):,}")
                                    print(f"  Total: {event.get('total_tokens'):,}")
                                
                                elif event_type == "title":
                                    print(f"📌 TITLE: {event.get('content')}")
                            
                            except json.JSONDecodeError:
                                pass
            
            # Store in conversation history
            self.conversation_history.append({
                "role": "user",
                "content": message
            })
            
            if result["full_answer"]:
                self.conversation_history.append({
                    "role": "assistant",
                    "content": result["full_answer"]
                })
            
            # Track token usage
            if result["usage"]:
                self.token_usage_history.append({
                    "turn": len(self.token_usage_history) + 1,
                    "message": message[:50],
                    "usage": result["usage"]
                })
            
        except Exception as e:
            print(f"\n❌ EXCEPTION: {e}")
            result["errors"].append(str(e))
            import traceback
            traceback.print_exc()
        
        return result
    
    async def run_deep_conversation(self, locale: str = "en", max_turns: int = 5):
        """Run a deep conversation following suggested questions"""
        print("\n" + "#"*80)
        print(f"# DEEP CONVERSATION TEST - {locale.upper()}")
        print(f"# Session: {self.session_id}")
        print(f"# Max turns: {max_turns}")
        print("#"*80)
        
        # Start with initial question
        initial_questions = {
            "en": "Show my sales today",
            "ar": "أظهر مبيعاتي اليوم",
            "tr": "Bugünkü satışlarımı göster"
        }
        
        initial_message = initial_questions.get(locale, initial_questions["en"])
        result = await self.send_message(initial_message, locale)
        
        # Follow suggested questions
        for turn in range(1, max_turns):
            if not result.get("suggestions") or len(result["suggestions"]) == 0:
                print(f"\n⚠️  No suggestions available, ending conversation")
                break
            
            # Pick first suggested question
            next_message = result["suggestions"][0]
            print(f"\n🔄 Following suggestion #{turn}...")
            await asyncio.sleep(1)  # Small delay
            
            result = await self.send_message(next_message, locale)
        
        # Print analysis
        self.print_analysis()
    
    def print_analysis(self):
        """Print detailed analysis of the conversation"""
        print("\n\n" + "="*80)
        print("CONVERSATION ANALYSIS")
        print("="*80)
        
        print(f"\n📊 Token Usage Per Turn:")
        print(f"{'Turn':<6} {'Message':<50} {'Input':<12} {'Output':<12} {'Total':<12}")
        print("-"*95)
        
        for entry in self.token_usage_history:
            usage = entry["usage"]
            print(f"{entry['turn']:<6} {entry['message']:<50} "
                  f"{usage.get('input_tokens', 0):>11,} "
                  f"{usage.get('output_tokens', 0):>11,} "
                  f"{usage.get('total_tokens', 0):>11,}")
        
        # Calculate totals and trends
        total_input = sum(e["usage"].get("input_tokens", 0) for e in self.token_usage_history)
        total_output = sum(e["usage"].get("output_tokens", 0) for e in self.token_usage_history)
        total_all = sum(e["usage"].get("total_tokens", 0) for e in self.token_usage_history)
        
        print("-"*95)
        print(f"{'TOTAL':<6} {'':<50} {total_input:>11,} {total_output:>11,} {total_all:>11,}")
        
        # Token growth analysis
        if len(self.token_usage_history) > 1:
            print(f"\n📈 Token Growth Analysis:")
            for i in range(1, len(self.token_usage_history)):
                prev_input = self.token_usage_history[i-1]["usage"].get("input_tokens", 0)
                curr_input = self.token_usage_history[i]["usage"].get("input_tokens", 0)
                growth = curr_input - prev_input
                growth_pct = (growth / prev_input * 100) if prev_input > 0 else 0
                
                print(f"  Turn {i} → {i+1}: Input tokens {prev_input:,} → {curr_input:,} "
                      f"(+{growth:,}, +{growth_pct:.1f}%)")
                
                if growth > 10000:
                    print(f"    ⚠️  WARNING: Large token increase detected!")
                if growth > 50000:
                    print(f"    🚨 CRITICAL: Massive token explosion!")
        
        # Conversation history size
        print(f"\n💬 Conversation History:")
        print(f"  Total messages: {len(self.conversation_history)}")
        total_chars = sum(len(msg["content"]) for msg in self.conversation_history)
        print(f"  Total characters: {total_chars:,} (~{total_chars//4:,} tokens)")
        
        # Identify issues
        print(f"\n🔍 Potential Issues:")
        issues_found = False
        
        for i, entry in enumerate(self.token_usage_history, 1):
            tokens = entry["usage"].get("input_tokens", 0)
            if tokens > 100000:
                print(f"  ❌ Turn {i}: Excessive input tokens ({tokens:,})")
                issues_found = True
            elif tokens > 50000:
                print(f"  ⚠️  Turn {i}: High input tokens ({tokens:,})")
                issues_found = True
        
        if not issues_found:
            print(f"  ✅ No token usage issues detected")
        
        print(f"\n{'='*80}\n")


async def main():
    """Main test runner"""
    parser = argparse.ArgumentParser(description="Deep conversation test for WAJ AI Agent")
    parser.add_argument("--url", default="http://localhost:8000", help="Base URL")
    parser.add_argument("--token", required=True, help="User JWT token")
    parser.add_argument("--business-id", required=True, help="Business ID")
    parser.add_argument("--locale", default="en", help="Language (en, ar, tr, ru)")
    parser.add_argument("--turns", type=int, default=5, help="Number of conversation turns")
    
    args = parser.parse_args()
    
    tester = DeepConversationTester(args.url, args.token, args.business_id)
    await tester.run_deep_conversation(args.locale, args.turns)


if __name__ == "__main__":
    asyncio.run(main())

