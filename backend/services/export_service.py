from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET
from typing import Iterable

LEAD_COLUMNS = [
    "name",
    "email",
    "phone",
    "personal_location",
    "ai_description",
    "work_field",
    "work_designation",
    "company_name",
    "company_location",
    "linkedin_url",
    "relevance_score",
    "activity_status",
    "raw_snippet",
]


def leads_to_csv(leads: Iterable[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=LEAD_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for lead in leads:
        row = {k: lead.get(k, "") or "" for k in LEAD_COLUMNS}
        writer.writerow(row)
    return buf.getvalue()


def leads_to_xml(leads: Iterable[dict], root_name: str = "leads") -> str:
    root = ET.Element(root_name)
    for lead in leads:
        item = ET.SubElement(root, "lead")
        for key in LEAD_COLUMNS:
            child = ET.SubElement(item, key)
            child.text = str(lead.get(key) or "")
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True)
