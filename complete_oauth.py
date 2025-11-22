#!/usr/bin/env python3
"""Complete OAuth flow with authorization code.

Use this after visiting the OAuth URL and getting the authorization code.
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

def complete_oauth(code: str):
    """Complete OAuth flow with authorization code."""
    # Get paths
    client_secrets_path = os.getenv(
        'GMAIL_CLIENT_SECRETS_PATH',
        'client_secret_729224522226-scuue33esjetj7b3qpkmjuekpmj036e5.apps.googleusercontent.com.json'
    )
    # Use a different filename if the default is a directory
    default_token = 'token_receiptagent.pickle'
    if os.path.exists(default_token) and os.path.isdir(default_token):
        token_file = 'token_receiptagent_new.pickle'
        print(f"⚠️  {default_token} is a directory, using {token_file} instead")
    else:
        token_file = os.getenv('GMAIL_TOKEN_PATH', default_token)
    
    if not os.path.exists(client_secrets_path):
        print(f"❌ Error: Client secrets file not found: {client_secrets_path}")
        print(f"   Please place the file in the project root")
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
    
    # Use manual OAuth flow
    flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
    
    try:
        print(f"🔄 Exchanging authorization code for token...")
        flow.fetch_token(code=code)
        # Get credentials from the flow (this is a Credentials object)
        creds = flow.credentials
        if not creds:
            raise ValueError("Failed to obtain credentials from OAuth flow")
        print("✅ Token received!")
        print(f"   Credentials type: {type(creds).__name__}")
    except Exception as e:
        print(f"❌ Error fetching token: {e}")
        sys.exit(1)
    
    # Save token (skip if it's a directory)
    if os.path.exists(token_file) and os.path.isdir(token_file):
        print(f"❌ Error: {token_file} is a directory, not a file!")
        print(f"   Please remove it manually: rm -rf {token_file}")
        sys.exit(1)
    
    # Save token (ensure it's a Credentials object, not OAuth2Token)
    from google.oauth2.credentials import Credentials as GoogleCredentials
    if not isinstance(creds, GoogleCredentials):
        print(f"⚠️  Warning: Expected Credentials object, got {type(creds).__name__}")
        print(f"   Attempting to convert...")
        # Try to create Credentials from token info
        if hasattr(creds, 'token'):
            token_info = {
                'token': creds.token if hasattr(creds, 'token') else None,
                'refresh_token': getattr(creds, 'refresh_token', None),
                'token_uri': 'https://oauth2.googleapis.com/token',
                'client_id': flow.client_config.get('client_id'),
                'client_secret': flow.client_config.get('client_secret'),
                'scopes': SCOPES
            }
            creds = GoogleCredentials(**{k: v for k, v in token_info.items() if v is not None})
        else:
            print(f"❌ Cannot convert {type(creds).__name__} to Credentials")
            sys.exit(1)
    
    # Save token
    with open(token_file, 'wb') as token:
        pickle.dump(creds, token)
    
    print()
    print(f"✅ Token saved to {token_file}!")
    print("   You can now use the Sync Inbox feature.")
    print()
    print("🔄 Next steps:")
    print("   1. Refresh your browser (http://localhost:8501)")
    print("   2. Click 'Sync Inbox' again")
    print("   3. It should work now! 🎉")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 complete_oauth.py <authorization_code>")
        print()
        print("Example:")
        print("  python3 complete_oauth.py 4/0AeanS8X...")
        sys.exit(1)
    
    code = sys.argv[1].strip()
    complete_oauth(code)

