"""Lists the Gemini models your key can call for text. Run from backend/:  python list_gemini_models.py"""
import httpx
from config import settings

r = httpx.get(f"{settings.gemini_base_url}/models", params={"pageSize": 200},
              headers={"x-goog-api-key": settings.gemini_api_key}, timeout=30)
print("HTTP", r.status_code)
if r.status_code == 200:
    names = sorted(m["name"].removeprefix("models/") for m in r.json().get("models", [])
                   if "generateContent" in m.get("supportedGenerationMethods", []) and "gemini" in m["name"])
    for n in names:
        print("  ", n)
    print("\nconfigured GEMINI_MODEL =", settings.gemini_model)
else:
    print(r.text[:300])
