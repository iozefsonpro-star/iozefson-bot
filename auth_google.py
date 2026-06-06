"""
Run this script ONCE in Railway Console to authorize Google Calendar.
Requires GOOGLE_CLIENT_SECRET_JSON environment variable to be set in Railway.

Steps:
  1. Set GOOGLE_CLIENT_SECRET_JSON in Railway Variables
  2. Run in Railway Console: python3 auth_google.py
  3. Open the URL shown, log in with Google, allow access
  4. Google shows a CODE — paste it back in Console
  5. Copy the printed token JSON
  6. Add it to Railway Variables as GOOGLE_TOKEN_JSON
"""

import json
import os
import tempfile
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
TOKEN_FILE = "token.json"

# Read client secret from env var or file
client_secret_json = os.environ.get("GOOGLE_CLIENT_SECRET_JSON")
if client_secret_json:
    # Write to temp file for the flow
    SECRET_FILE = tempfile.mktemp(suffix=".json")
    with open(SECRET_FILE, "w") as f:
        f.write(client_secret_json)
    print("Using credentials from GOOGLE_CLIENT_SECRET_JSON env var.")
else:
    SECRET_FILE = next(
        (f for f in os.listdir(".") if f.startswith("client_secret") and f.endswith(".json")),
        None,
    )
    if not SECRET_FILE:
        raise FileNotFoundError(
            "No credentials found.\n"
            "Set GOOGLE_CLIENT_SECRET_JSON in Railway Variables."
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
        flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
        auth_url, _ = flow.authorization_url(prompt="consent")
        print("\n🔗 Open this URL in your browser:\n")
        print(auth_url)
        print("\nAfter login Google will show you a CODE on screen.")
        print("Copy that code and paste it here:")
        code = input("\nPaste the code: ").strip()
        flow.fetch_token(code=code)
        creds = flow.credentials
        print("Authorization complete.")

with open(TOKEN_FILE, "w") as f:
    f.write(creds.to_json())

print(f"\n✅ token.json saved.")
print("\n📋 Copy the content below and paste it into Railway as GOOGLE_TOKEN_JSON:\n")
print(open(TOKEN_FILE).read())
