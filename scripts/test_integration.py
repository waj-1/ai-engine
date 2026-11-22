"""
Integration test script to verify backend-frontend connection.
Run this after starting the backend server.
"""

import requests
import json
import time
from datetime import datetime

# Configuration
BASE_URL = "http://localhost:8000"
TEST_TOKEN = "YOUR_JWT_TOKEN_HERE"  # Replace with valid JWT from Supabase

def test_health_check():
    """Test 1: Verify backend is running"""
    print("\n🔍 Test 1: Health Check")
    print("=" * 50)
    
    try:
        response = requests.get(f"{BASE_URL}/health", timeout=5)
        print(f"Status Code: {response.status_code}")
        print(f"Response: {json.dumps(response.json(), indent=2)}")
        
        assert response.status_code == 200, "Health check failed"
        assert response.json()["status"] == "healthy", "Server not healthy"
        
        print("✅ Health check passed")
        return True
    except Exception as e:
        print(f"❌ Health check failed: {e}")
        return False

def test_sse_stream():
    """Test 2: Verify SSE streaming works"""
    print("\n🔍 Test 2: SSE Streaming")
    print("=" * 50)
    
    if TEST_TOKEN == "YOUR_JWT_TOKEN_HERE":
        print("⚠️  Skipping: Please set a valid JWT token")
        return None
    
    try:
        headers = {
            "Authorization": f"Bearer {TEST_TOKEN}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "message": "Hello, what can you help me with?",
            "session_id": "550e8400-e29b-41d4-a716-446655440000",
            "business_id": "YOUR_BUSINESS_ID",  # Replace
            "locale": "en"
        }
        
        print(f"Sending request to: {BASE_URL}/chat/stream")
        print(f"Payload: {json.dumps(payload, indent=2)}")
        
        response = requests.post(
            f"{BASE_URL}/chat/stream",
            headers=headers,
            json=payload,
            stream=True,
            timeout=30
        )
        
        print(f"Status Code: {response.status_code}")
        
        if response.status_code != 200:
            print(f"❌ Request failed: {response.text}")
            return False
        
        print("\nStreaming events:")
        print("-" * 50)
        
        event_count = 0
        for line in response.iter_lines():
            if line:
                decoded_line = line.decode('utf-8')
                if decoded_line.startswith('data: '):
                    event_data = decoded_line[6:]  # Remove 'data: '
                    try:
                        event = json.loads(event_data)
                        event_count += 1
                        print(f"\nEvent {event_count}: {event.get('type', 'unknown')}")
                        print(f"  {json.dumps(event, indent=2)}")
                        
                        if event.get('type') == 'done':
                            break
                    except json.JSONDecodeError:
                        print(f"  Raw: {event_data}")
        
        print(f"\n✅ Received {event_count} events")
        return True
        
    except Exception as e:
        print(f"❌ SSE test failed: {e}")
        return False

def test_cors():
    """Test 3: Verify CORS configuration"""
    print("\n🔍 Test 3: CORS Configuration")
    print("=" * 50)
    
    try:
        response = requests.options(
            f"{BASE_URL}/chat/stream",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST"
            }
        )
        
        print(f"Status Code: {response.status_code}")
        print(f"CORS Headers:")
        for header, value in response.headers.items():
            if 'access-control' in header.lower():
                print(f"  {header}: {value}")
        
        print("✅ CORS test completed")
        return True
        
    except Exception as e:
        print(f"❌ CORS test failed: {e}")
        return False

def test_error_handling():
    """Test 4: Verify error handling"""
    print("\n🔍 Test 4: Error Handling")
    print("=" * 50)
    
    # Test without auth token
    print("\nTesting without authentication...")
    try:
        response = requests.post(
            f"{BASE_URL}/chat/stream",
            json={
                "message": "test",
                "session_id": "test",
                "business_id": "test"
            }
        )
        print(f"Status Code: {response.status_code}")
        assert response.status_code == 401, "Should return 401 without auth"
        print("✅ Correctly returns 401 without auth")
    except Exception as e:
        print(f"❌ Error test failed: {e}")
        return False
    
    # Test with invalid token
    print("\nTesting with invalid token...")
    try:
        response = requests.post(
            f"{BASE_URL}/chat/stream",
            headers={"Authorization": "Bearer invalid_token"},
            json={
                "message": "test",
                "session_id": "test",
                "business_id": "test"
            }
        )
        print(f"Status Code: {response.status_code}")
        assert response.status_code == 401, "Should return 401 with invalid token"
        print("✅ Correctly returns 401 with invalid token")
    except Exception as e:
        print(f"❌ Error test failed: {e}")
        return False
    
    return True

def main():
    """Run all integration tests"""
    print("\n" + "=" * 60)
    print("🚀 AI Chat Backend Integration Tests")
    print("=" * 60)
    print(f"Backend URL: {BASE_URL}")
    print(f"Test Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    results = []
    
    # Run tests
    results.append(("Health Check", test_health_check()))
    results.append(("CORS Configuration", test_cors()))
    results.append(("Error Handling", test_error_handling()))
    results.append(("SSE Streaming", test_sse_stream()))
    
    # Summary
    print("\n" + "=" * 60)
    print("📊 Test Summary")
    print("=" * 60)
    
    passed = sum(1 for _, result in results if result is True)
    failed = sum(1 for _, result in results if result is False)
    skipped = sum(1 for _, result in results if result is None)
    
    for test_name, result in results:
        status = "✅ PASSED" if result is True else "❌ FAILED" if result is False else "⏭️  SKIPPED"
        print(f"{test_name:.<40} {status}")
    
    print(f"\nTotal: {len(results)} tests")
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")
    print(f"Skipped: {skipped}")
    
    if failed == 0:
        print("\n🎉 All tests passed!")
    else:
        print(f"\n⚠️  {failed} test(s) failed")
    
    print("=" * 60)

if __name__ == "__main__":
    main()

