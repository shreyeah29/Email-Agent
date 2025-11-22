#!/usr/bin/env python3
"""Generate Gmail OAuth token for receipts-only account.

This script helps generate the token_receiptagent.pickle file
for the receipts-only Gmail account when running in Docker/headless environments.
"""
import os
import sys
import json
import pickle
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.modify'
]

def generate_token():
    """Generate Gmail OAuth token."""
    # Get paths
    client_secrets_path = os.getenv(
        'GMAIL_CLIENT_SECRETS_PATH',
        'client_secret_729224522226-scuue33esjetj7b3qpkmjuekpmj036e5.apps.googleusercontent.com.json'
    )
    token_file = os.getenv('GMAIL_TOKEN_PATH', 'token_receiptagent.pickle')
    
    if not os.path.exists(client_secrets_path):
        print(f"❌ Error: Client secrets file not found: {client_secrets_path}")
        print(f"   Please place the file in the project root or set GMAIL_CLIENT_SECRETS_PATH")
        sys.exit(1)
    
    # Load client config
    with open(client_secrets_path, 'r') as f:
        client_config = json.load(f)
    
    # Create flow
    if 'installed' in client_config:
        flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    elif 'web' in client_config:
        flow = InstalledAppFlow.from_client_config(
            {"installed": client_config['web']}, SCOPES
        )
    else:
        print("❌ Error: Invalid client secrets format")
        sys.exit(1)
    
    # Try browser flow first
    try:
        print("🌐 Opening browser for Gmail OAuth authentication...")
        print("   Please sign in with your receipts-only Gmail account and grant permissions.")
        print()
        creds = flow.run_local_server(port=0)
    except Exception as e:
        if "could not locate runnable browser" in str(e).lower():
            # Manual flow
            print("⚠️  Browser not available, using manual OAuth flow...")
            print()
            flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
            auth_url, _ = flow.authorization_url(prompt='consent')
            
            print("📋 Please visit this URL in your browser:")
            print(f"   {auth_url}")
            print()
            code = input("Enter the authorization code: ").strip()
            
            try:
                creds = flow.fetch_token(code=code)
            except Exception as fetch_error:
                print(f"❌ Error fetching token: {fetch_error}")
                sys.exit(1)
        else:
            print(f"❌ Error: {e}")
            sys.exit(1)
    
    # Save token
    with open(token_file, 'wb') as token:
        pickle.dump(creds, token)
    
    print()
    print(f"✅ Token saved to {token_file}!")
    print("   You can now use the Sync Inbox feature.")

if __name__ == "__main__":
    generate_token()

