"""Read numeric OpenRouter key usage only; never print credential/name data."""
import json
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[2]


def main():
    import sys
    sys.path.insert(0, str(ROOT))
    from core.llm.keystore import KeyStore
    store = KeyStore(path=ROOT / "memory/keys.json", secret_path=ROOT / "memory/.keystore_secret")
    entry = store.get_for_provider("openrouter", reveal=True)
    if not entry: raise RuntimeError("Configured OpenRouter key unavailable")
    response = httpx.get("https://openrouter.ai/api/v1/auth/key", headers={"Authorization":"Bearer " + entry["api_key"]}, timeout=20)
    response.raise_for_status()
    data = response.json()["data"]
    result = {key:data.get(key) for key in ("usage", "usage_daily", "limit", "limit_remaining")}
    result["scope"] = "Entire configured key; not attributable solely to this test run"
    result["daily_below_approved_test_cap"] = isinstance(result.get("usage_daily"), (int,float)) and result["usage_daily"] < .50
    result["lifetime_below_conservative_test_cap"] = isinstance(result.get("usage"), (int,float)) and result["usage"] < .50
    destination = ROOT / "qa/results/acceptance-20261001/budget.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__": main()
