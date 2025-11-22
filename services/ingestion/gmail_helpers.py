"""Gmail helper functions for candidate message fetching and processing.

Additive — does not modify existing behavior.
This module provides helper functions for Gmail API operations.
"""
import os
import json
import logging
import base64
from typing import List, Dict, Any, Optional
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/gmail.readonly', 'https://www.googleapis.com/auth/gmail.modify']


def get_gmail_service():
    """Get authenticated Gmail service, reusing existing token cache."""
    creds = None
    # Check environment variable first, then default
    token_file = os.getenv('GMAIL_TOKEN_PATH', 'token.json')
    
    # Try to load existing token
    if os.path.exists(token_file) and os.path.isfile(token_file):
        try:
            # Load token without scopes first to get the token's actual scopes
            import json
            with open(token_file, 'r') as f:
                token_data = json.load(f)
            token_scopes = token_data.get('scopes', SCOPES)
            # Use token's scopes or fallback to our required scopes
            creds = Credentials.from_authorized_user_file(token_file, token_scopes)
            logger.info(f"Loaded existing Gmail token from {token_file} with scopes: {token_scopes}")
        except Exception as e:
            logger.warning(f"Could not load existing token from {token_file}: {e}")
            # Try with our required scopes as fallback
            try:
                creds = Credentials.from_authorized_user_file(token_file, SCOPES)
                logger.info(f"Loaded token with fallback scopes")
            except:
                creds = None
    
    # Refresh or get new token
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                logger.info("Refreshing expired Gmail token...")
                creds.refresh(Request())
                # Save refreshed token
                with open(token_file, 'w') as token:
                    token.write(creds.to_json())
                logger.info("Token refreshed successfully")
            except Exception as e:
                logger.error(f"Token refresh failed: {e}")
                # If refresh fails, we still have the expired creds, try to use them
                # The API might still work if it's only slightly expired
                if not creds.refresh_token:
                    creds = None
        
        if not creds:
            # Try to load from shared settings first
            from shared import settings as app_settings
            
            client_id = app_settings.gmail_client_id or os.getenv('GMAIL_CLIENT_ID')
            client_secret = app_settings.gmail_client_secret or os.getenv('GMAIL_CLIENT_SECRET')
            
            if client_id and client_secret:
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
            else:
                # Try client secrets file
                client_secrets_path = os.getenv(
                    'GMAIL_CLIENT_SECRETS_PATH',
                    '/mnt/data/client_secret_256579107172-41mnqgf7c0q5kp8ebbnluao73901g2ve.apps.googleusercontent.com.json'
                )
                
                if os.path.exists(client_secrets_path):
                    flow = InstalledAppFlow.from_client_secrets_file(client_secrets_path, SCOPES)
                else:
                    raise ValueError("Gmail credentials not found. Set GMAIL_CLIENT_SECRETS_PATH or GMAIL_CLIENT_ID/SECRET")
            
            # Try to run OAuth flow (may fail in Docker without browser)
            try:
                creds = flow.run_local_server(port=0)
            except Exception as e:
                if "could not locate runnable browser" in str(e) or "browser" in str(e).lower():
                    raise ValueError(
                        "Gmail OAuth requires browser. Token not found or expired. "
                        "Please run 'python get_gmail_token.py' locally to generate token.json, "
                        "then mount it in Docker."
                    )
                raise
        
        # Save token for future use
        with open(token_file, 'w') as token:
            token.write(creds.to_json())
    
    try:
        service = build('gmail', 'v1', credentials=creds)
        return service
    except Exception as e:
        logger.error(f"Error building Gmail service: {e}")
        return None


def get_candidate_messages(query: str = "has:attachment subject:(invoice OR receipt OR bill)", max_results: int = 50) -> List[Dict[str, Any]]:
    """Fetch candidate messages from Gmail with metadata only (no body/attachments downloaded).
    
    Additive — does not modify existing behavior.
    
    Args:
        query: Gmail search query (default: "has:attachment subject:(invoice OR receipt OR bill)")
        max_results: Maximum number of messages to return
        
    Returns:
        List of message previews with metadata
    """
    service = get_gmail_service()
    if not service:
        return []
    
    try:
        # List messages
        results = service.users().messages().list(
            userId='me',
            q=query,
            maxResults=max_results
        ).execute()
        
        messages = results.get('messages', [])
        if not messages:
            return []
        
        # Get metadata for each message
        previews = []
        for msg in messages:
            try:
                # First, get basic metadata
                message = service.users().messages().get(
                    userId='me',
                    id=msg['id'],
                    format='metadata',
                    metadataHeaders=['From', 'Subject', 'Date']
                ).execute()
                
                # Extract headers
                headers = message.get('payload', {}).get('headers', [])
                subject = ""
                from_addr = ""
                date = ""
                
                for header in headers:
                    name = header.get('name', '').lower()
                    if name == 'subject':
                        subject = header.get('value', '')
                    elif name == 'from':
                        from_addr = header.get('value', '')
                    elif name == 'date':
                        date = header.get('value', '')
                
                # Check for attachments - use hasAttachment field if available
                has_attachment = message.get('payload', {}).get('hasAttachment', False)
                attachment_filenames = []
                
                # If hasAttachment is True or we need filenames, get full message parts
                if has_attachment or True:  # Always check for filenames
                    # Get full message to extract attachment filenames
                    full_message = service.users().messages().get(
                        userId='me',
                        id=msg['id'],
                        format='full'
                    ).execute()
                    
                    parts = full_message.get('payload', {}).get('parts', [])
                    if not parts:
                        parts = [full_message.get('payload', {})]
                    
                    def extract_attachments(part_list):
                        """Recursively extract attachment filenames."""
                        filenames = []
                        for part in part_list:
                            filename = part.get('filename', '')
                            if filename:
                                filenames.append(filename)
                            nested = part.get('parts', [])
                            if nested:
                                filenames.extend(extract_attachments(nested))
                        return filenames
                    
                    attachment_filenames = extract_attachments(parts)
                    has_attachment = len(attachment_filenames) > 0 or has_attachment
                
                previews.append({
                    "message_id": msg['id'],
                    "subject": subject,
                    "from": from_addr,
                    "date": date,
                    "snippet": message.get('snippet', ''),
                    "has_attachment": has_attachment,
                    "attachment_filenames": attachment_filenames
                })
            except HttpError as e:
                logger.error(f"Error fetching message {msg['id']}: {e}")
                continue
        
        return previews
    
    except HttpError as e:
        logger.error(f"Error listing messages: {e}")
        return []


def fetch_message_body_and_attachments(message_id: str, staging_dir: Optional[str] = None) -> Dict[str, Any]:
    """Download message body and attachments to staging directory.
    
    Additive — does not modify existing behavior.
    
    Args:
        message_id: Gmail message ID
        staging_dir: Directory to save attachments (default: temp directory)
        
    Returns:
        Dict with 'email_json', 'email_data', 'attachments' (list of file paths), 'raw_text', 'staging_dir'
    """
    service = get_gmail_service()
    if not service:
        raise ValueError("Gmail service not available")
    
    import tempfile
    if staging_dir is None:
        staging_dir = tempfile.mkdtemp(prefix='gmail_attachments_')
    else:
        os.makedirs(staging_dir, exist_ok=True)
    
    try:
        # Get full message
        message = service.users().messages().get(
            userId='me',
            id=message_id,
            format='full'
        ).execute()
        
        # Save email JSON
        email_json_path = os.path.join(staging_dir, f"{message_id}.json")
        with open(email_json_path, 'w') as f:
            json.dump(message, f, default=str)
        
        # Extract body text
        raw_text = ""
        parts = message.get('payload', {}).get('parts', [])
        if not parts:
            parts = [message.get('payload', {})]
        
        def extract_text(part):
            text = ""
            mime_type = part.get('mimeType', '').lower()
            body = part.get('body', {})
            data = body.get('data', '')
            
            if data and mime_type in ('text/plain', 'text/html'):
                try:
                    text = base64.urlsafe_b64decode(data).decode('utf-8', errors='ignore')
                except Exception as e:
                    logger.warning(f"Error decoding text: {e}")
            
            # Recursively check nested parts
            nested = part.get('parts', [])
            for nested_part in nested:
                text += "\n" + extract_text(nested_part)
            
            return text
        
        raw_text = extract_text(message.get('payload', {}))
        
        # Download attachments
        attachment_paths = []
        for part in parts:
            filename = part.get('filename', '')
            if filename:
                att_id = part.get('body', {}).get('attachmentId')
                if att_id:
                    try:
                        att = service.users().messages().attachments().get(
                            userId='me',
                            messageId=message_id,
                            id=att_id
                        ).execute()
                        data = base64.urlsafe_b64decode(att['data'])
                        
                        file_path = os.path.join(staging_dir, filename)
                        with open(file_path, 'wb') as f:
                            f.write(data)
                        attachment_paths.append(file_path)
                    except Exception as e:
                        logger.error(f"Error downloading attachment {filename}: {e}")
            
            # Check nested parts
            nested = part.get('parts', [])
            for nested_part in nested:
                nested_filename = nested_part.get('filename', '')
                if nested_filename:
                    nested_att_id = nested_part.get('body', {}).get('attachmentId')
                    if nested_att_id:
                        try:
                            att = service.users().messages().attachments().get(
                                userId='me',
                                messageId=message_id,
                                id=nested_att_id
                            ).execute()
                            data = base64.urlsafe_b64decode(att['data'])
                            
                            file_path = os.path.join(staging_dir, nested_filename)
                            with open(file_path, 'wb') as f:
                                f.write(data)
                            attachment_paths.append(file_path)
                        except Exception as e:
                            logger.error(f"Error downloading nested attachment {nested_filename}: {e}")
        
        return {
            "email_json": email_json_path,
            "email_data": message,
            "attachments": attachment_paths,
            "raw_text": raw_text,
            "staging_dir": staging_dir
        }
    
    except HttpError as e:
        logger.error(f"Error fetching message {message_id}: {e}")
        raise


def apply_label(message_id: str, label_name: str = "ProcessedByAgent") -> bool:
    """Create label if missing and apply it to message (non-destructive).
    
    Additive — does not modify existing behavior.
    Non-destructive: only adds labels, never deletes messages.
    
    Args:
        message_id: Gmail message ID
        label_name: Label name to apply
        
    Returns:
        True if successful, False otherwise
    """
    service = get_gmail_service()
    if not service:
        return False
    
    try:
        # Check if label exists, create if not
        labels = service.users().labels().list(userId='me').execute().get('labels', [])
        label_id = None
        
        for label in labels:
            if label['name'] == label_name:
                label_id = label['id']
                break
        
        if not label_id:
            # Create label
            new_label = service.users().labels().create(
                userId='me',
                body={'name': label_name, 'labelListVisibility': 'labelShow', 'messageListVisibility': 'show'}
            ).execute()
            label_id = new_label['id']
        
        # Apply label to message
        service.users().messages().modify(
            userId='me',
            id=message_id,
            body={'addLabelIds': [label_id]}
        ).execute()
        
        logger.info(f"Applied label '{label_name}' to message {message_id}")
        return True
    
    except HttpError as e:
        logger.error(f"Error applying label to message {message_id}: {e}")
        return False

