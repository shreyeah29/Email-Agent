"""API endpoints for candidate message review and processing.

Additive — does not modify existing behavior.
This module adds new endpoints for manual email review and processing.
"""
import logging
import uuid
import json
import os
from typing import List, Dict, Any, Optional
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel

from services.ingestion.gmail_helpers import get_candidate_messages, apply_label, get_gmail_service
from services.worker.message_adapter import process_message_by_id
from shared import settings, SessionLocal
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

try:
    from googleapiclient.errors import HttpError
except ImportError:
    # Fallback if googleapiclient not available
    class HttpError(Exception):
        def __init__(self, resp, content, *args, **kwargs):
            self.resp = resp
            self.content = content
            super().__init__(*args, **kwargs)

security = HTTPBearer()

def verify_api_key(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Verify API key from header."""
    if credentials.credentials != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return credentials.credentials

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/candidates", tags=["candidates"])

# Job store: Try DB first, fallback to file
JOBS_FILE = "jobs.json"
USE_DB = False
ProcessingJob = None

# Try to use DB table if available
try:
    from shared import Base
    from sqlalchemy import Column, Integer, Text, TIMESTAMP
    from sqlalchemy.dialects.postgresql import UUID, JSONB
    from sqlalchemy.sql import func
    
    class ProcessingJob(Base):
        """Processing job model for candidate message review.
        
        Additive — does not modify existing behavior.
        """
        __tablename__ = "processing_jobs"
        
        job_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
        message_id = Column(Text, nullable=False)
        status = Column(Text, nullable=False, default='queued')
        progress = Column(Integer, default=0)
        queued_at = Column(TIMESTAMP, server_default=func.now())
        started_at = Column(TIMESTAMP)
        finished_at = Column(TIMESTAMP)
        result_path = Column(Text)
        result = Column(JSONB)
        error_message = Column(Text)
        created_at = Column(TIMESTAMP, server_default=func.now())
    
    # Test if table exists
    db = SessionLocal()
    try:
        db.execute("SELECT 1 FROM processing_jobs LIMIT 1")
        USE_DB = True
        logger.info("Using database for job store")
    except Exception:
        logger.info("processing_jobs table not found, using file-based job store")
    finally:
        db.close()
except Exception as e:
    logger.warning(f"Could not use DB for job store, using file-based: {e}")

def load_job_store() -> Dict[str, Dict[str, Any]]:
    """Load job store from DB or file.
    
    Additive — does not modify existing behavior.
    """
    if USE_DB:
        try:
            db = SessionLocal()
            jobs = {}
            try:
                db_jobs = db.query(ProcessingJob).all()
                for job in db_jobs:
                    jobs[str(job.job_id)] = {
                        "job_id": str(job.job_id),
                        "message_id": job.message_id,
                        "status": job.status,
                        "progress": job.progress or 0,
                        "created_at": job.queued_at.isoformat() if job.queued_at else None,
                        "result": job.result
                    }
            finally:
                db.close()
            return jobs
        except Exception as e:
            logger.warning(f"Error loading jobs from DB, falling back to file: {e}")
    
    # Fallback to file
    if os.path.exists(JOBS_FILE):
        try:
            with open(JOBS_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Error loading job store: {e}")
    return {}

def save_job_store_entry(job_id: str, job_data: Dict[str, Any]):
    """Save single job entry to DB or file.
    
    Additive — does not modify existing behavior.
    """
    if USE_DB and ProcessingJob:
        try:
            db = SessionLocal()
            try:
                existing = db.query(ProcessingJob).filter(ProcessingJob.job_id == job_id).first()
                if existing:
                    existing.status = job_data.get('status', existing.status)
                    existing.progress = job_data.get('progress', existing.progress)
                    existing.result = job_data.get('result')
                    existing.finished_at = datetime.fromisoformat(job_data['finished_at']) if job_data.get('finished_at') else None
                else:
                    job = ProcessingJob(
                        job_id=job_id,
                        message_id=job_data['message_id'],
                        status=job_data.get('status', 'queued'),
                        progress=job_data.get('progress', 0),
                        result=job_data.get('result')
                    )
                    db.add(job)
                db.commit()
            finally:
                db.close()
            return
        except Exception as e:
            logger.warning(f"Error saving job to DB, falling back to file: {e}")
    
    # Fallback to file
    job_store = load_job_store()
    job_store[job_id] = job_data
    try:
        with open(JOBS_FILE, 'w') as f:
            json.dump(job_store, f, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error saving job store: {e}")

# Initialize job store
job_store: Dict[str, Dict[str, Any]] = load_job_store()


class ProcessMessagesRequest(BaseModel):
    """Request model for processing messages."""
    message_ids: List[str]
    label_after: bool = False


class ProcessMessagesResponse(BaseModel):
    """Response model for processing messages."""
    jobs: List[Dict[str, str]]
    queued_count: int


@router.get("/messages")
def get_candidate_messages_endpoint(
    q: str = Query(
        default="has:attachment subject:(invoice OR receipt OR bill)",
        description="Gmail search query"
    ),
    max: int = Query(default=50, ge=1, le=100, description="Maximum results"),
    api_key: str = Depends(verify_api_key)
):
    """Get candidate messages from Gmail with metadata previews.
    
    Additive — does not modify existing behavior.
    Returns list of message previews with subject, from, date, snippet,
    and attachment information. Does not download attachments.
    """
    try:
        messages = get_candidate_messages(query=q, max_results=max)
        return messages
    except ValueError as e:
        logger.error(f"Gmail authentication error: {e}")
        raise HTTPException(status_code=502, detail=f"Gmail authentication failed: {str(e)}")
    except Exception as e:
        logger.error(f"Error fetching candidate messages: {e}")
        raise HTTPException(status_code=502, detail=f"Error fetching messages: {str(e)}")


@router.post("/process")
def process_messages_endpoint(
    request: ProcessMessagesRequest,
    api_key: str = Depends(verify_api_key)
):
    """Process selected message IDs through the extraction pipeline.
    
    Additive — does not modify existing behavior.
    Validates each message ID exists in Gmail, then processes them.
    Returns job IDs for tracking progress. Implements idempotency checks.
    """
    if not request.message_ids:
        raise HTTPException(status_code=400, detail="message_ids cannot be empty")
    
    jobs = []
    processed_message_ids = set()
    
    # Check for duplicates in request
    seen_in_request = set()
    for msg_id in request.message_ids:
        if msg_id in seen_in_request:
            logger.warning(f"Duplicate message_id in request: {msg_id}")
            continue
        seen_in_request.add(msg_id)
    
    for message_id in request.message_ids:
        # Idempotency check: skip if already processed
        existing_job = None
        for job_id, job_data in job_store.items():
            if job_data.get('message_id') == message_id and job_data.get('status') == 'success':
                existing_job = job_data
                logger.info(f"Message {message_id} already processed, returning existing job")
                jobs.append({
                    "job_id": job_data['job_id'],
                    "message_id": message_id,
                    "status": "already_processed"
                })
                processed_message_ids.add(message_id)
                break
        
        if existing_job:
            continue
        
        # Validate message exists (quick check via Gmail API)
        try:
            service = get_gmail_service()
            if not service:
                raise HTTPException(status_code=502, detail="Gmail service not available")
            service.users().messages().get(userId='me', id=message_id, format='metadata').execute()
        except HttpError as e:
            if e.resp.status == 404:
                raise HTTPException(status_code=400, detail=f"Message {message_id} not found in Gmail")
            logger.warning(f"Message {message_id} validation failed: {e}")
        except Exception as e:
            logger.warning(f"Message {message_id} validation error: {e}")
        
        # Create job record
        job_id = str(uuid.uuid4())
        job_data = {
            "job_id": job_id,
            "message_id": message_id,
            "status": "queued",
            "progress": 0,
            "created_at": datetime.now().isoformat(),
            "result": None
        }
        job_store[job_id] = job_data
        save_job_store_entry(job_id, job_data)
        
        jobs.append({
            "job_id": job_id,
            "message_id": message_id,
            "status": "queued"
        })
        
        # Process asynchronously (in production, use background task queue)
        try:
            # Update status
            job_store[job_id]["status"] = "processing"
            job_store[job_id]["progress"] = 25
            job_store[job_id]["started_at"] = datetime.now().isoformat()
            save_job_store_entry(job_id, job_store[job_id])
            
            # Process message
            result = process_message_by_id(message_id)
            
            # Update job with result
            job_store[job_id]["status"] = result["status"]
            job_store[job_id]["progress"] = 100 if result["status"] == "success" else 0
            job_store[job_id]["result"] = result
            job_store[job_id]["finished_at"] = datetime.now().isoformat()
            save_job_store_entry(job_id, job_store[job_id])
            
            # Apply label if requested and successful
            if request.label_after and result["status"] == "success":
                try:
                    apply_label(message_id, "ProcessedByAgent")
                except Exception as e:
                    logger.warning(f"Failed to apply label to {message_id}: {e}")
        
        except Exception as e:
            logger.error(f"Error processing message {message_id}: {e}")
            job_store[job_id]["status"] = "failed"
            job_store[job_id]["progress"] = 0
            job_store[job_id]["result"] = {
                "message_id": message_id,
                "invoice_records": [],
                "summary_text": f"Processing failed: {str(e)}",
                "provenance_path": None,
                "status": "failed",
                "confidence": 0.0
            }
            job_store[job_id]["finished_at"] = datetime.now().isoformat()
            job_store[job_id]["error_message"] = str(e)
            save_job_store_entry(job_id, job_store[job_id])
    
    return ProcessMessagesResponse(
        jobs=jobs,
        queued_count=len(jobs)
    )


@router.get("/process_status")
def get_process_status_endpoint(
    job_id: Optional[str] = Query(None, description="Specific job ID to check"),
    api_key: str = Depends(verify_api_key)
):
    """Get processing status for one or all jobs.
    
    Additive — does not modify existing behavior.
    If job_id is provided, returns status for that job.
    Otherwise, returns status for all jobs.
    """
    # Reload job store to get latest status
    job_store.update(load_job_store())
    
    if job_id:
        if job_id not in job_store:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        return job_store[job_id]
    else:
        # Return all jobs
        return {"jobs": list(job_store.values())}

