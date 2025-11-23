"""Item categorization and BOM numbering using Ollama."""
import logging
from typing import List, Dict, Any
import json

try:
    import ollama
    OLLAMA_AVAILABLE = True
except ImportError:
    OLLAMA_AVAILABLE = False
    ollama = None

from shared import settings

logger = logging.getLogger(__name__)

# Common construction/industrial categories
DEFAULT_CATEGORIES = [
    "Electrical",
    "Hardware",
    "Tools",
    "Plumbing",
    "HVAC",
    "Materials",
    "Safety",
    "Fasteners",
    "Lumber",
    "Concrete",
    "Other"
]


def categorize_items_with_ollama(line_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Categorize line items using Ollama and assign BOM numbers.
    
    Args:
        line_items: List of line items with description, quantity, unit_price, subtotal
        
    Returns:
        List of categorized items with category and bom_number added
    """
    if not OLLAMA_AVAILABLE:
        logger.warning("Ollama not available, using default categorization")
        return _categorize_with_keywords(line_items)
    
    if not line_items:
        return []
    
    try:
        # Prepare items for categorization
        items_text = []
        for idx, item in enumerate(line_items):
            desc = item.get('description', '')
            items_text.append(f"{idx + 1}. {desc}")
        
        items_list = "\n".join(items_text)
        
        # Create prompt for Ollama
        system_prompt = """You are a construction materials categorization expert. 
Categorize each item into one of these categories:
- Electrical (wires, outlets, switches, boxes, breakers, etc.)
- Hardware (screws, nails, bolts, hinges, handles, etc.)
- Tools (hammers, drills, saws, wrenches, etc.)
- Plumbing (pipes, fittings, faucets, valves, etc.)
- HVAC (ducts, vents, filters, thermostats, etc.)
- Materials (lumber, drywall, insulation, etc.)
- Safety (gloves, helmets, goggles, etc.)
- Fasteners (screws, nails, bolts, washers, etc.)
- Lumber (wood, boards, plywood, etc.)
- Concrete (cement, aggregates, additives, etc.)
- Other (anything that doesn't fit above)

Return ONLY a JSON array where each element is: {"item_index": number, "category": "category_name"}
Example: [{"item_index": 1, "category": "Electrical"}, {"item_index": 2, "category": "Hardware"}]"""

        user_prompt = f"""Categorize these construction/industrial items:

{items_list}

Return ONLY the JSON array, no other text."""

        # Call Ollama
        client = ollama.Client(host=settings.ollama_base_url)
        response = client.generate(
            model=settings.ollama_model,
            prompt=user_prompt,
            system=system_prompt,
            options={
                "temperature": 0.1,  # Low temperature for consistent categorization
                "num_predict": 500
            }
        )
        
        # Extract JSON from response
        response_text = response.get('response', '')
        logger.info(f"Ollama categorization response: {response_text[:200]}...")
        
        # Try to extract JSON from response
        json_start = response_text.find('[')
        json_end = response_text.rfind(']') + 1
        if json_start >= 0 and json_end > json_start:
            json_text = response_text[json_start:json_end]
            categories = json.loads(json_text)
        else:
            # Fallback to keyword-based categorization
            logger.warning("Could not parse JSON from Ollama response, using keyword fallback")
            return _categorize_with_keywords(line_items)
        
        # Map categories to items and assign BOM numbers
        categorized_items = []
        category_bom_counter = {}  # Track BOM numbers per category
        
        for item in line_items:
            item_idx = line_items.index(item) + 1
            category = "Other"  # Default
            
            # Find matching category from Ollama response
            for cat_entry in categories:
                if cat_entry.get('item_index') == item_idx:
                    category = cat_entry.get('category', 'Other')
                    break
            
            # Validate category
            if category not in DEFAULT_CATEGORIES:
                category = "Other"
            
            # Assign BOM number (format: CAT-001, CAT-002, etc.)
            if category not in category_bom_counter:
                category_bom_counter[category] = 0
            category_bom_counter[category] += 1
            bom_number = f"{category[:3].upper()}-{category_bom_counter[category]:03d}"
            
            # Add category and BOM to item
            categorized_item = item.copy()
            categorized_item['category'] = category
            categorized_item['bom_number'] = bom_number
            categorized_items.append(categorized_item)
        
        logger.info(f"Successfully categorized {len(categorized_items)} items using Ollama")
        return categorized_items
        
    except Exception as e:
        logger.error(f"Error categorizing with Ollama: {e}")
        return _categorize_with_keywords(line_items)


def _categorize_with_keywords(line_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fallback keyword-based categorization."""
    categorized_items = []
    category_bom_counter = {}
    
    # Keyword mapping
    category_keywords = {
        "Electrical": ["wire", "outlet", "switch", "box", "breaker", "circuit", "electrical", "cable", "conduit", "octagon", "gang"],
        "Hardware": ["screw", "nail", "bolt", "hinge", "handle", "bracket", "bracket", "hardware"],
        "Tools": ["hammer", "drill", "saw", "wrench", "tool", "bit", "puller"],
        "Plumbing": ["pipe", "fitting", "faucet", "valve", "plumbing", "pvc"],
        "HVAC": ["duct", "vent", "filter", "thermostat", "hvac"],
        "Materials": ["drywall", "insulation", "material"],
        "Safety": ["glove", "helmet", "goggle", "safety"],
        "Fasteners": ["screw", "nail", "bolt", "washer", "fastener"],
        "Lumber": ["lumber", "wood", "board", "plywood", "2x4", "2x6"],
        "Concrete": ["cement", "concrete", "aggregate", "additive"],
    }
    
    for item in line_items:
        desc = str(item.get('description', '')).lower()
        category = "Other"
        
        # Find matching category
        for cat, keywords in category_keywords.items():
            if any(keyword in desc for keyword in keywords):
                category = cat
                break
        
        # Assign BOM number
        if category not in category_bom_counter:
            category_bom_counter[category] = 0
        category_bom_counter[category] += 1
        bom_number = f"{category[:3].upper()}-{category_bom_counter[category]:03d}"
        
        categorized_item = item.copy()
        categorized_item['category'] = category
        categorized_item['bom_number'] = bom_number
        categorized_items.append(categorized_item)
    
    return categorized_items

