"""
Test script to verify Gemini API is working correctly
"""

import os
from dotenv import load_dotenv
from google import genai

# Load environment
load_dotenv()

def test_gemini_basic():
    """Test basic Gemini API call"""
    api_key = os.getenv("GOOGLE_API_KEY")
    print(f"API Key loaded: {api_key[:20]}..." if api_key else "API Key NOT FOUND")
    
    if not api_key:
        print("❌ GOOGLE_API_KEY not found in environment")
        return False
    
    try:
        # Create client
        client = genai.Client(api_key=api_key)
        print("✅ Client created successfully")
        
        # Test simple generation with Gemini 3 Flash Preview model
        response = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents="Say hello in 5 words",
        )
        
        print(f"✅ Response received: {response.text}")
        return True
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_gemini_streaming():
    """Test Gemini streaming using correct API (generate_content_stream)"""
    api_key = os.getenv("GOOGLE_API_KEY")
    
    try:
        client = genai.Client(api_key=api_key)
        
        # Test streaming using the correct method: generate_content_stream()
        # Per SDK guidelines: Don't use stream=True, use dedicated streaming method
        print("Streaming response:")
        for chunk in client.models.generate_content_stream(
            model="gemini-3-flash-preview",
            contents="Count from 1 to 5"
        ):
            if hasattr(chunk, 'text') and chunk.text:
                print(f"  Chunk: {chunk.text}")
        
        print("✅ Streaming works!")
        return True
        
    except Exception as e:
        print(f"❌ Streaming error: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    print("=" * 50)
    print("Testing Gemini API Integration")
    print("=" * 50)
    
    print("\n1. Testing Basic Generation...")
    test_gemini_basic()
    
    print("\n2. Testing Streaming Generation...")
    test_gemini_streaming()
    
    print("\n" + "=" * 50)
    print("Tests Complete")
    print("=" * 50)

