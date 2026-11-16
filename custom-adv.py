import os
import sys
import requests
from datetime import datetime


# ============================================================
# ENVIRONMENT
# ============================================================

GITLAB_URL = "https://gitlab.com"

GITLAB_PROJECT_ID = os.environ["GITLAB_PROJECT_ID"]
GITLAB_TOKEN = os.environ["GITLAB_TOKEN"]

# DATE_CODE comes from workflow: YYYYMMDD
DATE_CODE = os.environ["DATE_CODE"]


# ============================================================
# DATE
# ============================================================

ADVANCE_DATE = datetime.strptime(
    DATE_CODE,
    "%Y%m%d"
).strftime("%Y-%m-%d")


# ============================================================
# TRIGGER PIPELINE
# ============================================================

def main():

    print(f"📅 DATE_CODE    : {DATE_CODE}")
    print(f"📅 ADVANCE_DATE : {ADVANCE_DATE}")
    print("")
    print("🚀 Triggering pipeline...")

    response = requests.post(
        f"{GITLAB_URL}/api/v4/projects/{GITLAB_PROJECT_ID}/pipeline",
        headers={
            "PRIVATE-TOKEN": GITLAB_TOKEN,
            "Content-Type": "application/json",
        },
        json={
            "ref": "main",
            "variables": [
                {
                    "key": "ADVANCE_DATE",
                    "value": ADVANCE_DATE,
                },
                {
                    "key": "ADVANCE_DAYS",
                    "value": "",
                },
            ],
        },
        timeout=30,
    )

    if not response.ok:
        print(f"❌ PIPELINE TRIGGER FAILED")
        print(f"   HTTP  : {response.status_code}")
        print(f"   Error : {response.text}")
        sys.exit(1)

    data = response.json()

    print("✅ PIPELINE TRIGGERED")
    print(f"   ID     : {data.get('id')}")
    print(f"   Status : {data.get('status')}")
    print(f"   URL    : {data.get('web_url')}")


if __name__ == "__main__":
    main()
