"""Reconciliation worker - matches invoices to vendors and projects."""
import logging
import time
from typing import Dict, List, Optional, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_
from rapidfuzz import fuzz, process

from shared import SessionLocal, Invoice, Vendor, Project

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class Reconciler:
    """Reconciles invoices with vendors and projects using fuzzy matching."""
    
    def __init__(self, db: Session):
        self.db = db
        self.vendors = self._load_vendors()
        self.projects = self._load_projects()
    
    def _load_vendors(self) -> List[Dict]:
        """Load all vendors with their aliases."""
        vendors = self.db.query(Vendor).all()
        result = []
        for v in vendors:
            names = [v.canonical_name]
            if v.aliases:
                names.extend(v.aliases)
            result.append({
                'vendor_id': v.vendor_id,
                'canonical_name': v.canonical_name,
                'all_names': names
            })
        return result
    
    def _load_projects(self) -> List[Dict]:
        """Load all projects with their codes."""
        projects = self.db.query(Project).all()
        result = []
        for p in projects:
            names = [p.name]
            if p.codes:
                names.extend(p.codes)
            result.append({
                'project_id': p.project_id,
                'name': p.name,
                'all_names': names
            })
        return result
    
    def match_vendor(self, vendor_name: str) -> Tuple[Optional[int], float, List[Dict]]:
        """Match vendor name using fuzzy matching. Returns (vendor_id, score, suggestions)."""
        if not vendor_name:
            return None, 0.0, []
        
        vendor_name = vendor_name.strip()
        best_match = None
        best_score = 0.0
        suggestions = []
        
        for vendor in self.vendors:
            for name in vendor['all_names']:
                score = fuzz.ratio(vendor_name.lower(), name.lower())
                if score > best_score:
                    best_score = score
                    best_match = vendor['vendor_id']
                
                if score >= 60:
                    suggestions.append({
                        'vendor_id': vendor['vendor_id'],
                        'name': vendor['canonical_name'],
                        'score': score
                    })
        
        # Sort suggestions by score
        suggestions = sorted(suggestions, key=lambda x: x['score'], reverse=True)[:3]
        
        return best_match, best_score, suggestions
    
    def match_project(self, project_name: str) -> Tuple[Optional[int], float, List[Dict]]:
        """Match project name using fuzzy matching."""
        if not project_name:
            return None, 0.0, []
        
        project_name = project_name.strip()
        best_match = None
        best_score = 0.0
        suggestions = []
        
        for project in self.projects:
            for name in project['all_names']:
                score = fuzz.ratio(project_name.lower(), name.lower())
                if score > best_score:
                    best_score = score
                    best_match = project['project_id']
                
                if score >= 60:
                    suggestions.append({
                        'project_id': project['project_id'],
                        'name': project['name'],
                        'score': score
                    })
        
        suggestions = sorted(suggestions, key=lambda x: x['score'], reverse=True)[:3]
        
        return best_match, best_score, suggestions
    
    def reconcile_invoice(self, invoice: Invoice) -> bool:
        """Reconcile a single invoice with vendors and projects."""
        extracted = invoice.extracted or {}
        normalized = invoice.normalized or {}
        
        vendor_name = None
        project_name = None
        
        # Get vendor name from extracted
        if 'vendor_name' in extracted:
            vendor_data = extracted['vendor_name']
            if isinstance(vendor_data, dict) and 'value' in vendor_data:
                vendor_name = str(vendor_data['value'])
        
        # Get project name from extracted (if present)
        if 'project_name' in extracted or 'project_code' in extracted:
            project_data = extracted.get('project_name') or extracted.get('project_code')
            if isinstance(project_data, dict) and 'value' in project_data:
                project_name = str(project_data['value'])
        
        updated = False
        reconciliation_status = invoice.reconciliation_status or 'needs_review'
        
        # Match vendor
        if vendor_name:
            vendor_id, vendor_score, vendor_suggestions = self.match_vendor(vendor_name)
            
            if vendor_score >= 90:
                normalized['vendor_id'] = vendor_id
                normalized['vendor_name'] = next(
                    v['canonical_name'] for v in self.vendors if v['vendor_id'] == vendor_id
                )
                reconciliation_status = 'auto_matched'
                updated = True
                logger.info(f"Invoice {invoice.invoice_id}: Auto-matched vendor {vendor_id} (score: {vendor_score})")
            elif vendor_score >= 60:
                # Store suggestions in extra
                if not invoice.extra:
                    invoice.extra = {}
                if 'suggestions' not in invoice.extra:
                    invoice.extra['suggestions'] = {}
                invoice.extra['suggestions']['vendors'] = vendor_suggestions
                updated = True
        
        # Match project
        if project_name:
            project_id, project_score, project_suggestions = self.match_project(project_name)
            
            if project_score >= 90:
                normalized['project_id'] = project_id
                normalized['project_name'] = next(
                    p['name'] for p in self.projects if p['project_id'] == project_id
                )
                updated = True
                logger.info(f"Invoice {invoice.invoice_id}: Auto-matched project {project_id} (score: {project_score})")
            elif project_score >= 60:
                if not invoice.extra:
                    invoice.extra = {}
                if 'suggestions' not in invoice.extra:
                    invoice.extra['suggestions'] = {}
                invoice.extra['suggestions']['projects'] = project_suggestions
                updated = True
        
        # Update normalized totals if available
        if 'total_amount' in extracted:
            total_data = extracted['total_amount']
            if isinstance(total_data, dict) and 'value' in total_data:
                normalized['total_amount'] = total_data['value']
                if 'currency' in total_data:
                    normalized['currency'] = total_data['currency']
                updated = True
        
        if 'date' in extracted:
            date_data = extracted['date']
            if isinstance(date_data, dict) and 'value' in date_data:
                normalized['date'] = date_data['value']
                updated = True
        
        if updated:
            invoice.normalized = normalized
            invoice.reconciliation_status = reconciliation_status
        
        return updated


def run_reconciler_worker():
    """Main reconciliation worker loop."""
    logger.info("Starting reconciliation worker...")
    
    while True:
        try:
            db = SessionLocal()
            try:
                reconciler = Reconciler(db)
                
                # Find invoices that need reconciliation
                invoices = db.query(Invoice).filter(
                    or_(
                        Invoice.reconciliation_status == 'needs_review',
                        Invoice.reconciliation_status.is_(None)
                    )
                ).limit(50).all()
                
                if invoices:
                    logger.info(f"Processing {len(invoices)} invoices for reconciliation...")
                    reconciled_count = 0
                    
                    for invoice in invoices:
                        if reconciler.reconcile_invoice(invoice):
                            reconciled_count += 1
                    
                    db.commit()
                    logger.info(f"Reconciled {reconciled_count} invoices")
                else:
                    logger.debug("No invoices need reconciliation")
                
            finally:
                db.close()
            
            # Sleep for 30 seconds before next batch
            time.sleep(30)
            
        except KeyboardInterrupt:
            logger.info("Reconciliation worker stopped")
            break
        except Exception as e:
            logger.error(f"Error in reconciliation worker: {e}")
            time.sleep(30)


if __name__ == "__main__":
    run_reconciler_worker()

