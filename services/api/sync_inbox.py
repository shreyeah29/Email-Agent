"""Sync Inbox API endpoint for receipts-only Gmail account.

Additive — does not modify existing behavior.
This endpoint processes all emails with attachments from a receipts-only inbox.
"""
import os
import logging
from typing import Optional, Dict, Any
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy.orm import Session

from shared import get_db, Invoice, settings
from services.ingestion.gmail_sync import (
    build_gmail_service,
    search_messages,
    download_message_and_attachments,
    apply_label
)
from services.worker.message_adapter import process_message_by_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sync_inbox", tags=["sync"])
security = HTTPBearer()


def verify_api_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Verify API key from header."""
    if credentials.credentials != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return credentials.credentials


class SyncRequest(BaseModel):
    """Request model for sync inbox endpoint."""
    max: Optional[int] = 100  # Maximum emails to process per sync
    include_processed: Optional[bool] = False  # If True, include emails with ProcessedByAgent label


class SyncResponse(BaseModel):
    """Response model for sync inbox endpoint."""
    total_found: int
    processed: int
    skipped: int
    errors: int
    new_invoices: int
    message_ids: list[str] = []


def sync_inbox_internal(max_results: int = 100, include_processed: bool = False) -> Dict[str, Any]:
    """Internal function to sync inbox - can be called by scheduler or API endpoint.
    
    Args:
        max_results: Maximum number of emails to process
        include_processed: If True, include emails with ProcessedByAgent label
        
    Returns:
        Dict with sync results
    """
    from shared import SessionLocal
    
    db = SessionLocal()
    try:
        # Build Gmail service for receipts account
        logger.info("Building Gmail service for receipts account...")
        service = build_gmail_service()
        
        # Build search query for receipts-only inbox
        # has:attachment AND filename:(pdf OR xls OR xlsx OR jpg OR jpeg OR png)
        # Optionally exclude ProcessedByAgent label if include_processed is False
        base_query = "has:attachment filename:(pdf OR xls OR xlsx OR jpg OR jpeg OR png)"
        if not include_processed:
            query = f"{base_query} -label:ProcessedByAgent"
        else:
            query = base_query
            logger.info("Including emails with ProcessedByAgent label (re-processing mode)")
        
        # Search for candidate messages
        logger.info(f"Searching for messages with query: {query}")
        message_ids = search_messages(service, query, max_results=max_results)
        
        total_found = len(message_ids)
        processed = 0
        skipped = 0
        errors = 0
        new_invoices = 0
        processed_message_ids = []
        
        # Process each message
        for message_id in message_ids:
            try:
                # Check if already processed (idempotency)
                existing = db.query(Invoice).filter(Invoice.source_email_id == message_id).first()
                if existing:
                    logger.info(f"Message {message_id} already processed, skipping")
                    skipped += 1
                    continue
                
                # Download message and attachments
                staging_dir = f"data/staging/sync/{message_id}"
                Path(staging_dir).mkdir(parents=True, exist_ok=True)
                
                logger.info(f"Downloading message {message_id}...")
                email_data = download_message_and_attachments(service, message_id, staging_dir)
                
                # Check if we have any attachments
                if not email_data.get('attachments'):
                    logger.warning(f"Message {message_id} has no valid attachments, skipping")
                    skipped += 1
                    continue
                
                # Process message through extraction pipeline
                logger.info(f"Processing message {message_id} through extraction pipeline...")
                result = process_message_by_id(message_id, force=False)
                
                if result.get('status') == 'success':
                    processed += 1
                    processed_message_ids.append(message_id)
                    
                    # Check if new invoice was created
                    if not result.get('already_processed', False):
                        new_invoices += 1
                    
                    # Apply label to mark as processed
                    try:
                        apply_label(service, message_id, "ProcessedByAgent")
                        logger.info(f"Applied ProcessedByAgent label to {message_id}")
                    except Exception as label_error:
                        logger.warning(f"Could not apply label to {message_id}: {label_error}")
                        # Don't fail the whole sync if labeling fails
                else:
                    logger.error(f"Processing failed for {message_id}: {result.get('summary_text')}")
                    errors += 1
                
            except Exception as e:
                logger.error(f"Error processing message {message_id}: {e}")
                errors += 1
                continue
        
        logger.info(
            f"Sync complete: found={total_found}, processed={processed}, "
            f"skipped={skipped}, errors={errors}, new_invoices={new_invoices}"
        )
        
        result = {
            "total_found": total_found,
            "processed": processed,
            "skipped": skipped,
            "errors": errors,
            "new_invoices": new_invoices,
            "message_ids": processed_message_ids[:10]  # Return first 10 for reference
        }
        
        return result
    
    except Exception as e:
        logger.error(f"Sync failed: {e}")
        raise
    finally:
        db.close()


@router.post("", response_model=SyncResponse)
def sync_inbox(
    request: SyncRequest,
    db: Session = Depends(get_db),
    api_key: str = Depends(verify_api_key)
):
    """Sync receipts-only inbox - process all emails with attachments.
    
    Additive — does not modify existing behavior.
    This endpoint:
    1. Searches for unprocessed emails with attachments
    2. Downloads email JSON and attachments
    3. Processes through extraction pipeline
    4. Marks emails as processed with label
    
    Args:
        request: Sync request with optional max limit
        db: Database session
        
    Returns:
        SyncResponse with counts and statistics
    """
    try:
        result = sync_inbox_internal(
            max_results=request.max or 100,
            include_processed=request.include_processed or False
        )
        
        return SyncResponse(**result)
    
    except Exception as e:
        logger.error(f"Sync inbox error: {e}")
        raise HTTPException(status_code=500, detail=f"Sync failed: {str(e)}")

