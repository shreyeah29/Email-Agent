#!/bin/bash
# Simple script to view invoices from the API

echo "📊 Fetching invoices from API..."
echo ""

curl -s -H "Authorization: Bearer dev-api-key" \
  "http://localhost:8000/invoices?page=1&page_size=5" | \
  python3 -m json.tool

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "To see a specific invoice, use:"
echo "  curl -H 'Authorization: Bearer dev-api-key' http://localhost:8000/invoice/{invoice_id}"
echo ""
echo "Or use the Swagger UI at: http://localhost:8000/docs"

