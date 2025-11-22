#!/usr/bin/env python3
"""Script to generate Gmail OAuth token."""
import os
import sys
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly', 'https://www.googleapis.com/auth/gmail.modify']

def get_token():
    """Get Gmail OAuth token."""
    creds = None
    token_file = 'token.json'
    
    # Check if token already exists
    if os.path.exists(token_file) and os.path.isfile(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
        if creds and creds.valid:
            print("✅ Token already exists and is valid!")
            return
    
    # Load client credentials from environment or use client config
    client_id = os.getenv("GMAIL_CLIENT_ID")
    client_secret = os.getenv("GMAIL_CLIENT_SECRET")
    
    if not client_id or not client_secret:
        print("❌ Error: GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET must be set in .env")
        sys.exit(1)
    
    # Create OAuth flow
    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"]
            }
        },
        SCOPES
    )
    
    print("🌐 Opening browser for Gmail OAuth authentication...")
    print("   Please sign in and grant permissions.")
    print()
    
    # Run OAuth flow (will open browser)
    creds = flow.run_local_server(port=0)
    
    # Save token
    with open(token_file, 'w') as token:
        token.write(creds.to_json())
    
    print()
    print("✅ Token saved to token.json!")
    print("   You can now use this token with the ingestion service.")

if __name__ == "__main__":
    # Load .env file
    from dotenv import load_dotenv
    load_dotenv()
    
    get_token()

