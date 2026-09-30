"""Lists the model ids your Groq key can use. Run from backend/:  python list_groq_models.py"""
import httpx
from config import settings

r = httpx.get(f"{settings.groq_base_url}/models",
              headers={"Authorization": f"Bearer {settings.groq_api_key}"}, timeout=30)
print("HTTP", r.status_code)
if r.status_code == 200:
    for m in sorted(x["id"] for x in r.json().get("data", [])):
        print("  ", m)
    print("\nconfigured: live =", settings.groq_live_model, "| analysis =", settings.groq_analysis_model,
          "| agent =", settings.groq_agent_model)
else:
    print(r.text[:300])
