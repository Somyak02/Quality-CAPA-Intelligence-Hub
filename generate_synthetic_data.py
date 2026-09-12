from __future__ import annotations

import random
from datetime import datetime, timedelta

import pandas as pd


random.seed(42)

product_lines = ["DRAM", "NAND", "SSD Assembly", "Wafer Fab", "Packaging"]
categories = [
    "Contamination",
    "Equipment Calibration",
    "Documentation Error",
    "Process Deviation",
    "Supplier Quality",
    "Testing Failure",
    "Packaging Defect",
    "Non-Conforming Material",
]

severity_map = {"High": 0.35, "Medium": 0.45, "Low": 0.20}

incident_templates = [
    "Batch #{lot} failed moisture sensitivity test after an operator skipped a required handling step during {product_line} processing.",
    "Particle contamination was observed in the cleanroom after a filter issue caused airborne particles to exceed control limits in {product_line}.",
    "Calibration drift was observed on {equipment} causing measurements to shift by {offset} microns during routine verification.",
    "Lot traceability was unclear for {product_line} materials because the work instruction revision on the floor did not match the current approved document.",
    "Supplier {supplier} delivered incoming material showing defects beyond acceptance criteria in the {product_line} process line.",
    "Final test station reported inconsistent outcomes for the same unit during repeat testing on a {product_line} production lot.",
    "Moisture barrier seal integrity failed for a packaging batch and created a potential control breach in finished goods.",
    "Quarantined material was missing the non-conforming label and was at risk of unintended release into the next process step.",
]

supplier_names = ["Supplier A", "Supplier B", "Supplier C", "Supplier D"]
equipment_names = ["Metrology Tool M-22", "Furnace A-4", "Ion Implanter I-9", "Burn-in Chamber 4", "Torque Station T-12"]

rows = []
start_date = datetime(2026, 1, 1)

for i in range(1, 41):
    product_line = random.choice(product_lines)
    category = random.choice(categories)
    severity = random.choices(list(severity_map.keys()), weights=list(severity_map.values()), k=1)[0]
    lot = random.randint(1000, 9999)
    day_offset = random.randint(0, 120)
    ticket_date = (start_date + timedelta(days=day_offset)).strftime("%Y-%m-%d")
    equipment = random.choice(equipment_names)
    offset = round(random.uniform(0.1, 0.8), 2)
    supplier = random.choice(supplier_names)

    description = random.choice(incident_templates).format(
        lot=lot,
        product_line=product_line,
        equipment=equipment,
        offset=offset,
        supplier=supplier,
    )

    rows.append(
        {
            "ticket_id": f"INC-{1000 + i}",
            "date": ticket_date,
            "product_line": product_line,
            "category": category,
            "description": description,
            "true_severity": severity,
            "true_category": category,
        }
    )

pd.DataFrame(rows).to_csv("input_docs/quality_agent_project_data/incident_tickets.csv", index=False)
print(f"Generated {len(rows)} synthetic tickets.")
