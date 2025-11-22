#!/usr/bin/env python3
"""Clear all invoices and related data from the database."""
import os
import sys
from sqlalchemy import text
from shared import SessionLocal, Invoice, InvoiceAudit, Vendor, Project

def clear_all_data():
    """Clear all invoices, audit records, vendors, and projects."""
    db = SessionLocal()
    try:
        print("🗑️  Clearing all data...")
        
        # Delete in order (respecting foreign keys)
        print("  • Deleting invoice audit records...")
        db.execute(text("DELETE FROM invoice_audit"))
        
        print("  • Deleting invoices...")
        db.execute(text("DELETE FROM invoices"))
        
        print("  • Deleting vendors...")
        db.execute(text("DELETE FROM vendors"))
        
        print("  • Deleting projects...")
        db.execute(text("DELETE FROM projects"))
        
        db.commit()
        print("✅ All data cleared successfully!")
        return True
    except Exception as e:
        db.rollback()
        print(f"❌ Error clearing data: {e}")
        return False
    finally:
        db.close()

if __name__ == "__main__":
    confirm = input("⚠️  This will delete ALL invoices, vendors, and projects. Continue? (yes/no): ")
    if confirm.lower() == "yes":
        clear_all_data()
    else:
        print("❌ Cancelled.")

