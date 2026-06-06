"""
Run this script ONCE locally on your Mac to authorize Google Calendar access.
It will open a browser, ask you to log in with Google, and save token.json.

Steps:
  1. Place client_secret_*.json in the same folder as this script
  2. Run: python auth_google.py
  3. Log in with iozefson.pro@gmail.com in the browser
  4. Copy the content of token.json
  5. Add it to Railway Variables as GOOGLE_TOKEN_JSON (paste the full JSON string)
"""

import json
import os
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
TOKEN_FILE  = "token.json"
SECRET_FILE = next(
    (f for f in os.listdir(".") if f.startswith("client_secret") and f.endswith(".json")),
    None,
)

if not SECRET_FILE:
    raise FileNotFoundError(
        "client_secret_*.json not found in current directory.\n"
        "Download it from Google Cloud Console → APIs & Services → Credentials."
    )

print(f"Using credentials file: {SECRET_FILE}")

creds = None
if os.path.exists(TOKEN_FILE):
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

if not creds or not creds.valid:
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        print("Token refreshed.")
    else:
        flow = InstalledAppFlow.from_client_secrets_file(SECRET_FILE, SCOPES)
        creds = flow.run_local_server(port=0)
        print("Authorization complete.")

with open(TOKEN_FILE, "w") as f:
    f.write(creds.to_json())

print(f"\n✅ token.json saved.")
print("\n📋 Copy the content below and paste it into Railway as GOOGLE_TOKEN_JSON:\n")
print(open(TOKEN_FILE).read())
