"""Search filter definitions — extend when Zainab shares the full filter list."""

from __future__ import annotations

FILTER_DEFINITIONS = {
    "location": {"label": "Location", "type": "text", "placeholder": "City, region, or country"},
    "industry": {
        "label": "Industry / work field",
        "type": "select",
        "options": [
            "Technology",
            "Healthcare",
            "Finance",
            "Retail",
            "Education",
            "Real Estate",
            "Manufacturing",
            "Hospitality",
            "Marketing & Advertising",
            "Professional Services",
        ],
    },
    "job_titles": {
        "label": "Job titles",
        "type": "tags",
        "suggestions": [
            "Founder",
            "CEO",
            "CMO",
            "Marketing Manager",
            "Owner",
            "Director",
            "VP Sales",
            "Head of Growth",
        ],
    },
    "company_size": {
        "label": "Company size",
        "type": "select",
        "options": ["1-10", "11-50", "51-200", "201-500", "501-1000", "1000+"],
    },
    "company_location": {"label": "Company location", "type": "text", "placeholder": "HQ or office location"},
    "age_min": {"label": "Audience age (min)", "type": "number", "min": 13, "max": 100, "default": 25},
    "age_max": {"label": "Audience age (max)", "type": "number", "min": 13, "max": 100, "default": 45},
    "gender": {
        "label": "Gender focus",
        "type": "select",
        "options": ["Any", "Female", "Male", "Non-binary", "Unspecified"],
    },
    "language": {
        "label": "Language",
        "type": "select",
        "options": ["English", "Spanish", "French", "German", "Portuguese", "Hindi", "Arabic", "Chinese"],
    },
    "income_level": {
        "label": "Spend / income tier",
        "type": "select",
        "options": ["Budget-conscious", "Mid-market", "Premium", "Enterprise"],
    },
    "interests": {
        "label": "Interests",
        "type": "tags",
        "suggestions": [
            "Sustainability",
            "E-commerce",
            "SaaS",
            "Fitness",
            "B2B",
            "Local services",
            "Creators",
        ],
    },
    "seniority": {
        "label": "Seniority",
        "type": "select",
        "options": ["Entry", "Mid", "Senior", "Director", "VP", "C-Level", "Owner"],
    },
    "active_only": {"label": "Active LinkedIn users only", "type": "boolean", "default": True},
    "business_only": {"label": "Business-associated only", "type": "boolean", "default": True},
    "max_results": {"label": "Max results", "type": "number", "min": 3, "max": 100, "default": 25},
}
