"""Read-only connectivity preflight. Never print secret values or raw errors."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / ".deps"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gcloud", required=True)
    args = parser.parse_args()
    def secret(name):
        result = subprocess.run([args.gcloud, "secrets", "versions", "access", "latest",
            "--secret", name, "--project", "btc-bot-v1-live"], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError("Secret access failed: " + name)
        return result.stdout.strip()
    from coinbase.rest import RESTClient
    from paper_account import plain
    key = secret("COINBASE_DASHBOARD_KEY")
    pem = secret("COINBASE_DASHBOARD_SECRET").replace("\\n", "\n")
    client = RESTClient(api_key=key, api_secret=pem, timeout=12)
    permissions = plain(client.get_api_key_permissions())
    print(json.dumps({"read_only_key": permissions.get("can_trade") is False and permissions.get("can_transfer") is False,
        "can_view": permissions.get("can_view"), "can_trade": permissions.get("can_trade"), "can_transfer": permissions.get("can_transfer")}))
    product = plain(client.get_product("BIP-20DEC30-CDE"))
    details = product.get("future_product_details") or {}
    print(json.dumps({"product": product.get("product_id"), "price": product.get("price"),
        "status": product.get("status"), "funding_rate": details.get("funding_rate"),
        "funding_time": details.get("funding_time"), "session": details.get("trading_session_details"),
        "future_detail_keys": list(details)}))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("Preflight failed: " + type(exc).__name__)
        sys.exit(1)
