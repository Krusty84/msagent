"""Quick test: verify=False SSL bypass with DeepSeek API.

Usage:
    python3 test_verify_fix.py <deepseek_api_key>
    or: DEEPSEEK_API_KEY=xxx python3 test_verify_fix.py
"""

import os
import sys

api_key = sys.argv[1] if len(sys.argv) > 1 else os.getenv("DEEPSEEK_API_KEY", "")

if not api_key:
    print("SKIP: need DEEPSEEK_API_KEY as arg or env var")
    sys.exit(0)

try:
    import httpx
except ImportError:
    print("ERROR: httpx not installed. Run: pip install httpx")
    sys.exit(1)

# Simulate what the factory now does by default: verify=False
print("Testing with verify=False (new default)...")
client = httpx.Client(verify=False, timeout=10)
try:
    resp = client.get(
        "https://api.deepseek.com/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    print(f"  Status: {resp.status_code}")
    print(f"  Body (first 200 chars): {resp.text[:200]}")
    print("PASS")
except Exception as e:
    print(f"  FAIL: {type(e).__name__}: {e}")
finally:
    client.close()

# Also test with verify=True for comparison
print("\nTesting with verify=True (old default)...")
client2 = httpx.Client(verify=True, timeout=10)
try:
    resp2 = client2.get(
        "https://api.deepseek.com/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    print(f"  Status: {resp2.status_code}")
    print(f"  Body (first 200 chars): {resp2.text[:200]}")
    print("PASS")
except Exception as e:
    print(f"  FAIL: {type(e).__name__}: {e}")
finally:
    client2.close()
