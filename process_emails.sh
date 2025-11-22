#!/bin/bash
# Script to manually process invoice-related emails from Gmail

echo "🔍 Scanning Gmail for invoice-related emails..."
echo ""

# Run ingestion in manual mode (processes once and exits)
docker-compose -f infra/docker-compose.yml run --rm ingestion python services/ingestion/main.py

echo ""
echo "✅ Processing complete!"
echo ""
echo "Check the dashboard at http://localhost:8501 to see processed invoices."
