"""Publish explicitly selected dashboard secrets without values in argv or logs."""
import argparse
import subprocess

from dotenv import dotenv_values

NAMES = ("OPENAI_USAGE_API_KEY", "XAI_USAGE_API_KEY", "BYTEPLUS_BILLING_ACCESS_KEY_ID",
         "BYTEPLUS_BILLING_SECRET_ACCESS_KEY", "ELEVENLABS_USAGE_API_KEY",
         "GATEWAY_REPORTING_DATABASE_URL", "MAGICLENS_REPORTING_DATABASE_URL")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--project", default="ai-gateway-495414")
    args = parser.parse_args()
    values = dotenv_values(args.env_file)
    for name in NAMES:
        if not values.get(name):
            continue
        secret = "provider-usage-dashboard-" + name.lower().replace("_", "-")
        base = ["gcloud", "--quiet", "--project", args.project, "secrets"]
        exists = subprocess.run([*base, "describe", secret], capture_output=True)
        if exists.returncode:
            subprocess.run([*base, "create", secret, "--replication-policy=automatic"], check=True)
        subprocess.run([*base, "versions", "add", secret, "--data-file=-"], input=values[name].encode(), check=True)
        print(f"Published reporting secret: {name}")


if __name__ == "__main__":
    main()
