"""Scheduler for automatic Gmail ingestion."""
import logging
import threading
import time
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from shared import settings
# Import will be done inside function to avoid circular imports

logger = logging.getLogger(__name__)

# Global scheduler instance
_scheduler: Optional[BackgroundScheduler] = None
_is_running = False
_lock = threading.Lock()


def sync_inbox_internal_wrapper():
    """Wrapper for sync_inbox_internal that handles errors gracefully."""
    global _is_running
    
    with _lock:
        if _is_running:
            logger.warning("⏸️  Previous Gmail ingestion job still running, skipping this cycle")
            return
        _is_running = True
    
    try:
        logger.info("🔁 Scheduled Gmail ingestion job started")
        # Import here to avoid circular imports
        from services.api.sync_inbox import sync_inbox_internal
        result = sync_inbox_internal(max_results=100, include_processed=False)
        logger.info(f"✔ Gmail ingestion job completed: found={result.get('total_found', 0)}, processed={result.get('processed', 0)}")
    except Exception as e:
        logger.error(f"❌ Gmail ingestion job failed: {e}", exc_info=True)
    finally:
        with _lock:
            _is_running = False


def start_scheduler():
    """Start the background scheduler for Gmail ingestion."""
    global _scheduler
    
    if _scheduler and _scheduler.running:
        logger.warning("Scheduler already running")
        return
    
    interval_minutes = settings.gmail_ingest_interval_minutes
    logger.info(f"🚀 Starting Gmail ingestion scheduler (interval: {interval_minutes} minutes)")
    
    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        sync_inbox_internal_wrapper,
        trigger=IntervalTrigger(minutes=interval_minutes),
        id='gmail_ingestion',
        name='Gmail Ingestion Job',
        replace_existing=True
    )
    
    _scheduler.start()
    logger.info(f"✅ Scheduler started successfully. Will run every {interval_minutes} minutes.")


def stop_scheduler():
    """Stop the background scheduler."""
    global _scheduler
    
    if _scheduler and _scheduler.running:
        _scheduler.shutdown()
        logger.info("Scheduler stopped")


def get_scheduler_status() -> dict:
    """Get scheduler status."""
    if not _scheduler or not _scheduler.running:
        return {"status": "stopped", "interval_minutes": settings.gmail_ingest_interval_minutes}
    
    jobs = _scheduler.get_jobs()
    next_run = jobs[0].next_run_time if jobs else None
    
    return {
        "status": "running",
        "interval_minutes": settings.gmail_ingest_interval_minutes,
        "next_run": next_run.isoformat() if next_run else None,
        "is_running": _is_running
    }

