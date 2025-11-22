"""Gmail sync helpers for receipts-only inbox synchronization.

Additive — does not modify existing behavior.
This module provides helper functions specifically for the Sync Inbox feature
that processes all emails with attachments from a receipts-only Gmail account.
"""
import os
import json
import logging
import base64
import pickle
from typing import List, Dict, Any, Optional
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from shared import settings

logger = logging.getLogger(__name__)

# Check if we're in a headless environment (Docker)
def is_headless():
    """Check if running in headless environment (no display/browser)."""
    return os.getenv("DISPLAY") is None and os.getenv("DOCKER_ENV") == "true"

# Check if we're in a headless environment (Docker)
def is_headless():
    """Check if running in headless environment (no display/browser)."""
    return os.getenv("DISPLAY") is None and os.getenv("DOCKER_ENV") == "true"


def build_gmail_service() -> Any:
    """Build authenticated Gmail service for receipts-only account.
    
    Additive — uses separate credentials path and token for receipts account.
    Uses GMAIL_CLIENT_SECRETS_PATH and GMAIL_TOKEN_PATH from environment.
    
    Returns:
        Gmail API service object
        
    Raises:
        Exception if authentication fails
    """
    creds = None
    default_token = settings.gmail_token_path or os.getenv("GMAIL_TOKEN_PATH", "token_receiptagent.pickle")
    # If default token is a directory, use alternative filename
    if os.path.exists(default_token) and os.path.isdir(default_token):
        token_file = "token_receiptagent_new.pickle"
        logger.warning(f"{default_token} is a directory, using {token_file} instead")
    else:
        token_file = default_token
    # Use new receipts account client secrets - default to project directory
    client_secrets_path = (
        os.getenv("GMAIL_CLIENT_SECRETS_PATH") or 
        settings.gmail_client_secrets_path or
        "/app/client_secret_729224522226-scuue33esjetj7b3qpkmjuekpmj036e5.apps.googleusercontent.com.json"
    )
    
    if not client_secrets_path:
        raise ValueError("GMAIL_CLIENT_SECRETS_PATH must be set for Sync Inbox feature")
    
    if not os.path.exists(client_secrets_path):
        raise FileNotFoundError(f"Gmail client secrets file not found: {client_secrets_path}")
    
    # Parse scopes from env or use default
    scopes_str = settings.gmail_scopes or os.getenv("GMAIL_SCOPES", "https://www.googleapis.com/auth/gmail.readonly")
    scopes = [s.strip() for s in scopes_str.split(",") if s.strip()]
    if not scopes:
        scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
    
    # Add modify scope if not present (needed for labels)
    if "https://www.googleapis.com/auth/gmail.modify" not in scopes:
        scopes.append("https://www.googleapis.com/auth/gmail.modify")
    
    # Try to load existing token
    if os.path.exists(token_file):
        try:
            # Try pickle format first
            if token_file.endswith('.pickle'):
                with open(token_file, 'rb') as token:
                    loaded_obj = pickle.load(token)
                    # Check if it's a Credentials object, not OAuth2Token
                    if isinstance(loaded_obj, Credentials):
                        creds = loaded_obj
                    else:
                        # Old token format - need to regenerate
                        logger.warning(f"Token file contains {type(loaded_obj).__name__} instead of Credentials. Please regenerate token.")
                        creds = None
            else:
                # Try JSON format
                creds = Credentials.from_authorized_user_file(token_file, scopes)
            if creds:
                logger.info(f"Loaded existing Gmail token from {token_file}")
        except Exception as e:
            logger.warning(f"Could not load existing token from {token_file}: {e}")
            creds = None
    
    # Refresh or get new token
    if not creds or (creds.expired if hasattr(creds, 'expired') else True):
        if creds and hasattr(creds, 'expired') and creds.expired and hasattr(creds, 'refresh_token') and creds.refresh_token:
            try:
                logger.info("Refreshing expired Gmail token...")
                creds.refresh(Request())
                # Save refreshed token
                if token_file.endswith('.pickle'):
                    with open(token_file, 'wb') as token:
                        pickle.dump(creds, token)
                else:
                    with open(token_file, 'w') as token:
                        token.write(creds.to_json())
                logger.info("Token refreshed successfully")
            except Exception as e:
                logger.error(f"Token refresh failed: {e}")
                creds = None
        
        if not creds:
            # Load client secrets and create flow
            with open(client_secrets_path, 'r') as f:
                client_config = json.load(f)
            
            # Handle both 'installed' and 'web' client configs
            if 'installed' in client_config:
                flow = InstalledAppFlow.from_client_config(client_config, scopes)
            elif 'web' in client_config:
                # For web apps, we need to use OAuth2 flow differently
                flow = InstalledAppFlow.from_client_config(
                    {"installed": client_config['web']}, scopes
                )
            else:
                raise ValueError("Invalid client secrets format")
            
            # Check if we're in headless environment (Docker)
            if is_headless() or os.getenv("DOCKER_ENV") == "true":
                # Use manual OAuth flow for Docker/headless
                logger.info("Running in headless environment, using manual OAuth flow...")
                flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
                auth_url, _ = flow.authorization_url(prompt='consent')
                
                # Return helpful error with instructions
                error_msg = (
                    f"OAuth authentication required for receipts-only account.\n\n"
                    f"Since we're running in Docker (no browser), please complete authentication manually:\n\n"
                    f"1. Visit this URL in your browser:\n   {auth_url}\n\n"
                    f"2. Authorize the application and copy the authorization code\n\n"
                    f"3. Run this command locally (replace YOUR_CODE with the code):\n"
                    f"   python3 generate_receipts_token.py\n"
                    f"   (Or use the code when prompted)\n\n"
                    f"Alternatively, run this to generate token locally:\n"
                    f"   python3 generate_receipts_token.py"
                )
                raise ValueError(error_msg)
            
            # Try normal OAuth flow (with browser)
            try:
                creds = flow.run_local_server(port=0)
            except Exception as e:
                # Handle "no browser" error - provide manual OAuth URL
                if "could not locate runnable browser" in str(e).lower() or "browser" in str(e).lower():
                    logger.info("Browser not available, using manual OAuth flow...")
                    flow.redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
                    auth_url, _ = flow.authorization_url(prompt='consent')
                    error_msg = (
                        f"OAuth requires browser. Please visit this URL to authorize:\n"
                        f"{auth_url}\n\n"
                        f"After authorization, run: python3 generate_receipts_token.py"
                    )
                    raise ValueError(error_msg)
                else:
                    logger.error(f"OAuth flow failed: {e}")
                    raise
            
            # Save token
            if token_file.endswith('.pickle'):
                with open(token_file, 'wb') as token:
                    pickle.dump(creds, token)
            else:
                with open(token_file, 'w') as token:
                    token.write(creds.to_json())
            logger.info(f"Saved new token to {token_file}")
    
    # Build service
    service = build('gmail', 'v1', credentials=creds)
    logger.info("Gmail service built successfully for receipts account")
    return service


def search_messages(service: Any, query: str, max_results: int = 100) -> List[str]:
    """Search for Gmail messages matching query.
    
    Additive — does not modify existing behavior.
    
    Args:
        service: Gmail API service object
        query: Gmail search query string
        max_results: Maximum number of message IDs to return
        
    Returns:
        List of message IDs
    """
    try:
        message_ids = []
        page_token = None
        
        while len(message_ids) < max_results:
            request = service.users().messages().list(
                userId='me',
                q=query,
                maxResults=min(100, max_results - len(message_ids)),
                pageToken=page_token
            )
            response = request.execute()
            
            messages = response.get('messages', [])
            message_ids.extend([msg['id'] for msg in messages])
            
            page_token = response.get('nextPageToken')
            if not page_token:
                break
        
        logger.info(f"Found {len(message_ids)} messages matching query: {query}")
        return message_ids[:max_results]
    
    except HttpError as e:
        logger.error(f"Error searching messages: {e}")
        raise


def download_message_and_attachments(
    service: Any,
    message_id: str,
    staging_dir: str
) -> Dict[str, Any]:
    """Download full message and attachments to staging directory.
    
    Additive — does not modify existing behavior.
    Only downloads attachments matching: pdf, xls, xlsx, jpg, jpeg, png
    
    Args:
        service: Gmail API service object
        message_id: Gmail message ID
        staging_dir: Directory to save attachments
        
    Returns:
        Dict with:
        - raw_email_path: Path to saved raw email JSON
        - attachments: List of dicts with filename, mime, path, size
        - headers: Dict with From, Subject, Date
    """
    Path(staging_dir).mkdir(parents=True, exist_ok=True)
    
    try:
        # Get full message
        message = service.users().messages().get(
            userId='me',
            id=message_id,
            format='full'
        ).execute()
        
        # Save raw email JSON
        email_json_path = os.path.join(staging_dir, f"{message_id}.json")
        with open(email_json_path, 'w') as f:
            json.dump(message, f, indent=2)
        
        # Extract headers
        headers = {}
        for header in message.get('payload', {}).get('headers', []):
            name = header.get('name', '').lower()
            if name in ['from', 'subject', 'date']:
                headers[header['name']] = header.get('value', '')
        
        # Extract attachments
        attachments = []
        allowed_extensions = ['.pdf', '.xls', '.xlsx', '.jpg', '.jpeg', '.png']
        
        def extract_attachments(parts, path_prefix=''):
            """Recursively extract attachments from message parts."""
            att_list = []
            for part in parts:
                part_id = part.get('partId', '')
                filename = part.get('filename', '')
                mime_type = part.get('mimeType', '')
                
                # Check if this part is an attachment
                if filename:
                    # Check if file extension is allowed
                    file_ext = os.path.splitext(filename.lower())[1]
                    if file_ext in allowed_extensions:
                        att_id = part.get('body', {}).get('attachmentId')
                        if att_id:
                            try:
                                # Download attachment
                                att_data = service.users().messages().attachments().get(
                                    userId='me',
                                    messageId=message_id,
                                    id=att_id
                                ).execute()
                                
                                # Decode attachment data
                                file_data = base64.urlsafe_b64decode(att_data['data'])
                                
                                # Save to staging directory
                                safe_filename = filename.replace('/', '_').replace('\\', '_')
                                att_path = os.path.join(staging_dir, safe_filename)
                                with open(att_path, 'wb') as f:
                                    f.write(file_data)
                                
                                att_list.append({
                                    "filename": filename,
                                    "mime": mime_type,
                                    "path": att_path,
                                    "size": len(file_data)
                                })
                                logger.info(f"Downloaded attachment: {filename} ({len(file_data)} bytes)")
                            except Exception as e:
                                logger.warning(f"Error downloading attachment {filename}: {e}")
                
                # Recursively check nested parts
                nested_parts = part.get('parts', [])
                if nested_parts:
                    att_list.extend(extract_attachments(nested_parts))
            
            return att_list
        
        # Get all parts
        payload = message.get('payload', {})
        parts = payload.get('parts', [])
        if not parts:
            parts = [payload]
        
        attachments = extract_attachments(parts)
        
        return {
            "raw_email_path": email_json_path,
            "attachments": attachments,
            "headers": headers
        }
    
    except HttpError as e:
        logger.error(f"Error downloading message {message_id}: {e}")
        raise


def apply_label(service: Any, message_id: str, label_name: str = "ProcessedByAgent") -> None:
    """Create label if missing and apply it to message.
    
    Additive — does not modify existing behavior.
    Only adds labels; never deletes or modifies messages.
    
    Args:
        service: Gmail API service object
        message_id: Gmail message ID
        label_name: Name of label to apply
    """
    try:
        # Get or create label
        labels = service.users().labels().list(userId='me').execute().get('labels', [])
        label_id = None
        
        for label in labels:
            if label['name'] == label_name:
                label_id = label['id']
                break
        
        # Create label if it doesn't exist
        if not label_id:
            new_label = service.users().labels().create(
                userId='me',
                body={'name': label_name, 'labelListVisibility': 'labelShow', 'messageListVisibility': 'show'}
            ).execute()
            label_id = new_label['id']
            logger.info(f"Created label: {label_name}")
        
        # Apply label to message
        service.users().messages().modify(
            userId='me',
            id=message_id,
            body={'addLabelIds': [label_id]}
        ).execute()
        
        logger.info(f"Applied label '{label_name}' to message {message_id}")
    
    except HttpError as e:
        logger.error(f"Error applying label to message {message_id}: {e}")
        raise

