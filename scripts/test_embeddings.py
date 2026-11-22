#!/usr/bin/env python3
"""Test Google AI embeddings"""

import os
from google import genai

GOOGLE_API_KEY = "REDACTED_OPENAI_KEY"

client = genai.Client(api_key=GOOGLE_API_KEY)

# Test embedding
test_text = "This is a test document about booking management in WAJ."

try:
    print(f"Testing embedding with text: {test_text[:50]}...")
    result = client.models.embed_content(
        model="models/text-embedding-004",
        contents=test_text
    )
    
    print(f"✅ Success!")
    print(f"Embedding type: {type(result)}")
    print(f"Has embeddings: {hasattr(result, 'embeddings')}")
    
    if hasattr(result, 'embeddings'):
        print(f"Number of embeddings: {len(result.embeddings)}")
        if len(result.embeddings) > 0:
            emb = result.embeddings[0]
            print(f"First embedding type: {type(emb)}")
            print(f"Has values: {hasattr(emb, 'values')}")
            if hasattr(emb, 'values'):
                print(f"Embedding dimension: {len(emb.values)}")
                print(f"First 5 values: {emb.values[:5]}")
            else:
                print(f"Embedding attributes: {dir(emb)}")
    else:
        print(f"Result attributes: {dir(result)}")
        
except Exception as e:
    print(f"❌ Error: {e}")
    import traceback
    traceback.print_exc()

