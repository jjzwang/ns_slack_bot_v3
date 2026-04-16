import json
import pytest
from reviewer import (
    core_pillars_ready,
    merge_pillars,
    _parse_extraction_json,
    _parse_review_json
)

def test_core_pillars_ready():
    # True case
    ready_pillars = {
        "persona": "Accountant",
        "action": "Add a checkbox",
        "goal": "Prevent mistakes",
        "business_value": "Save time"
    }
    assert core_pillars_ready(ready_pillars) is True

    # False cases
    missing_pillars = ready_pillars.copy()
    missing_pillars["persona"] = None
    assert core_pillars_ready(missing_pillars) is False

    empty_pillars = {}
    assert core_pillars_ready(empty_pillars) is False

def test_merge_pillars():
    existing = {"persona": "Accountant", "action": None}
    new_extraction = {"persona": None, "action": "Add field"}
    
    merged = merge_pillars(existing, new_extraction)
    
    # Existing values shouldn't be overwritten by None
    assert merged["persona"] == "Accountant"
    # Null values should be updated by new extractions
    assert merged["action"] == "Add field"

def test_parse_extraction_json_valid():
    valid_json = """
    ```json
    {
      "persona": "Finance Team",
      "action": "Create validation",
      "goal": null,
      "business_value": "Reduce risk"
    }
    ```
    """
    result = _parse_extraction_json(valid_json)
    assert result["persona"] == "Finance Team"
    assert result["action"] == "Create validation"
    assert result["goal"] is None
    assert result["business_value"] == "Reduce risk"

def test_parse_extraction_json_malformed():
    malformed = "This isn't JSON at all."
    result = _parse_extraction_json(malformed)
    # Should safely return an empty baseline structure rather than crashing
    assert all(value is None for value in result.values())

def test_parse_review_json_valid():
    valid_review = """
    {
      "gaps": [
        {
          "pillar": "action",
          "severity": "high",
          "gap": "Missing record type",
          "suggested_question": "What record?"
        }
      ],
      "enrichments": [
        {
          "pillar": "description",
          "category": "implementation_approach",
          "detail": "Use a User Event script.",
          "confidence": "high"
        }
      ]
    }
    """
    gaps, enrichments = _parse_review_json(valid_review)
    assert len(gaps) == 1
    assert gaps[0]["pillar"] == "action"
    
    assert len(enrichments) == 1
    assert enrichments[0]["category"] == "implementation_approach"

def test_parse_review_json_validation_strips_bad_data():
    invalid_review = """
    {
      "gaps": [
        {
          "pillar": "fake_pillar",
          "severity": "extreme",
          "gap": "Bad severity and pillar",
          "suggested_question": "..."
        }
      ]
    }
    """
    gaps, enrichments = _parse_review_json(invalid_review)
    
    # The gap should be discarded entirely due to the invalid pillar 'fake_pillar'
    assert len(gaps) == 0