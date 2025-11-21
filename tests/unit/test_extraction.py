"""Unit tests for extraction logic."""
import pytest
from services.extractor.worker import InvoiceExtractor


class TestInvoiceExtractor:
    """Test invoice field extraction."""
    
    def test_extract_invoice_number(self):
        """Test invoice number extraction."""
        extractor = InvoiceExtractor()
        text = "Invoice Number: INV-2025-123"
        result = extractor.extract_field('invoice_number', text)
        
        assert result is not None
        assert 'INV-2025-123' in str(result['value'])
        assert result['confidence'] > 0
    
    def test_extract_date(self):
        """Test date extraction."""
        extractor = InvoiceExtractor()
        text = "Invoice Date: 10/21/2025"
        result = extractor.extract_field('date', text)
        
        assert result is not None
        assert '10' in str(result['value']) or '21' in str(result['value'])
    
    def test_extract_total_amount(self):
        """Test total amount extraction."""
        extractor = InvoiceExtractor()
        text = "Total Amount: 11,210.00"
        result = extractor.extract_field('total_amount', text)
        
        assert result is not None
        assert isinstance(result['value'], (int, float))
        assert result['value'] > 0
    
    def test_extract_vendor_name(self):
        """Test vendor name extraction."""
        extractor = InvoiceExtractor()
        text = "ACME Supplies Pvt Ltd\nInvoice Number: INV-123"
        result = extractor.extract_field('vendor_name', text)
        
        assert result is not None
        assert 'ACME' in str(result['value'])
    
    def test_extract_all_fields(self):
        """Test extracting all fields from sample text."""
        extractor = InvoiceExtractor()
        text = """
        ACME Supplies Pvt Ltd
        Invoice Number: INV-2025-123
        Date: 10/21/2025
        Total Amount: 11,210.00 INR
        """
        
        extracted = extractor.extract_all_fields(text, [])
        
        assert 'vendor_name' in extracted or 'invoice_number' in extracted
        assert len(extracted) > 0

