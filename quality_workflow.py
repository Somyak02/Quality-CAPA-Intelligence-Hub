from __future__ import annotations

import json
import re
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import faiss
import numpy as np
import pandas as pd
from langgraph.graph import END, START, StateGraph
from sentence_transformers import SentenceTransformer
from transformers import pipeline
from typing_extensions import TypedDict


ROOT_DIR = Path(__file__).resolve().parent
DOCS_DIR = ROOT_DIR / "input_docs" / "quality_agent_project_data" / "quality_sops"
TICKETS_PATH = ROOT_DIR / "input_docs" / "quality_agent_project_data" / "incident_tickets.csv"
LEGACY_TICKETS_PATH = ROOT_DIR / "input_docs" / "incident_tickets.csv"
TICKETS_EXCEL_PATH = ROOT_DIR / "input_docs" / "quality_agent_project_data" / "incident_tickets.xlsx"
ALLOWED_SEVERITIES = {"High", "Medium", "Low"}
ALLOWED_CATEGORIES = {
    "Contamination",
    "Equipment Calibration",
    "Documentation Error",
    "Process Deviation",
    "Supplier Quality",
    "Testing Failure",
    "Packaging Defect",
    "Non-Conforming Material",
}
CATEGORY_ALIASES = {
    "equipment": "Equipment Calibration",
    "calibration": "Equipment Calibration",
    "documentation": "Documentation Error",
    "process": "Process Deviation",
    "supplier": "Supplier Quality",
    "testing": "Testing Failure",
    "test": "Testing Failure",
    "packaging": "Packaging Defect",
    "nonconforming material": "Non-Conforming Material",
    "non-conforming material": "Non-Conforming Material",
}


class TicketState(TypedDict, total=False):
    ticket: Dict[str, Any]
    severity: str
    category: str
    retrieved_context: List[Dict[str, str]]
    draft: str
    status: str
    review_decision: str
    final_output: str
    triage_accuracy: float
    time_to_draft_sec: float


class LocalLLM:
    def __init__(self, model_name: str = "google/flan-t5-base"):
        self.model_name = model_name
        self.generator = None
        self._try_load()

    def _try_load(self) -> None:
        try:
            self.generator = pipeline("text2text-generation", model=self.model_name, tokenizer=self.model_name, device=-1)
        except Exception:
            self.generator = None

    def generate(self, prompt: str, max_new_tokens: int = 200) -> str:
        if self.generator is None:
            return ""
        try:
            output = self.generator(prompt, max_new_tokens=max_new_tokens, do_sample=False)
            return output[0]["generated_text"].strip()
        except Exception:
            return ""


class QualityWorkflow:
    def __init__(self, docs_dir: Path = DOCS_DIR, tickets_path: Path = TICKETS_PATH):
        self.docs_dir = docs_dir
        self.tickets_path = tickets_path
        self.excel_persistence_available = True
        self.ticket_df = pd.read_csv(tickets_path)
        self.ticket_df = self._ensure_ticket_status_column()
        self._triage_cache: Dict[Tuple[str, str], Dict[str, str]] = {}
        self.doc_chunks: List[Dict[str, str]] = self._load_documents()
        self.model = SentenceTransformer("BAAI/bge-base-en-v1.5")
        self.llm = LocalLLM()
        self.index, self.embeddings = self._build_vector_index()
        self.graph = self._build_graph()

    def _ensure_ticket_status_column(self) -> pd.DataFrame:
        df = self.ticket_df.copy()
        if "status" not in df.columns:
            df["status"] = None
        df["status"] = df["status"].astype("object")
        df["status"] = df["status"].where(pd.notna(df["status"]), None)
        if "status" in df.columns:
            self._persist_ticket_data(df)
        return df

    def _persist_ticket_data(self, df: pd.DataFrame) -> None:
        df.to_csv(self.tickets_path, index=False)
        if self.tickets_path != LEGACY_TICKETS_PATH:
            df.to_csv(LEGACY_TICKETS_PATH, index=False)
        try:
            df.to_excel(TICKETS_EXCEL_PATH, index=False)
        except ModuleNotFoundError as error:
            if error.name != "openpyxl":
                raise
            self.excel_persistence_available = False

    def _next_ticket_id(self) -> str:
        numbers = []
        for value in self.ticket_df.get("ticket_id", pd.Series(dtype=object)).astype(str):
            match = re.fullmatch(r"INC-(\d+)", value.strip(), flags=re.IGNORECASE)
            if match:
                numbers.append(int(match.group(1)))
        return f"INC-{max(numbers, default=1000) + 1:04d}"

    def create_ticket(self, description: str, product_line: str = "General", ticket_date: Optional[str] = None) -> Dict[str, Any]:
        cleaned_description = description.strip()
        if not cleaned_description:
            raise ValueError("A ticket description is required.")

        ticket = {
            "ticket_id": self._next_ticket_id(),
            "date": ticket_date or date.today().isoformat(),
            "product_line": product_line.strip() or "General",
            "description": cleaned_description,
            "true_severity": "",
            "true_category": "",
            "status": None,
        }
        ticket["category"] = self.triage_ticket(ticket)["category"]
        self.ticket_df = pd.concat([self.ticket_df, pd.DataFrame([ticket])], ignore_index=True)
        self._persist_ticket_data(self.ticket_df)
        return ticket

    def update_ticket_status(self, ticket_id: str, status: str) -> None:
        df = self.ticket_df.copy()
        if "ticket_id" not in df.columns:
            return
        if "status" not in df.columns:
            df["status"] = None
        df["status"] = df["status"].astype("object")
        status_value = str(status).strip().lower() if status is not None else None
        df.loc[df["ticket_id"] == ticket_id, "status"] = status_value
        self.ticket_df = df
        self._persist_ticket_data(self.ticket_df)

    def get_metrics(self, status_filter: Optional[str] = None) -> Dict[str, float]:
        if self.ticket_df.empty:
            return {
                "total_tickets": 0,
                "open_tickets": 0,
                "triage_accuracy": 0.0,
                "severity_accuracy": 0.0,
                "category_accuracy": 0.0,
                "avg_time_to_draft_sec": 0.0,
                "tickets_processed": 0,
            }

        df = self.ticket_df.copy()
        if "status" not in df.columns:
            df["status"] = None
        if status_filter == "non_approved":
            df = df[(df["status"].isna()) | (df["status"].astype(str).str.strip().str.lower() == "rejected")]
        elif status_filter == "approved":
            df = df[df["status"].astype(str).str.strip().str.lower() == "approved"]

        total = len(self.ticket_df)
        open_tickets = int(((df["status"].isna()) | (df["status"].astype(str).str.strip().str.lower() == "rejected")).sum()) if "status" in df.columns else total

        correct = 0
        correct_severity = 0
        correct_category = 0
        labeled_rows = self.ticket_df[
            self.ticket_df["true_severity"].notna()
            & self.ticket_df["true_category"].notna()
            & self.ticket_df["true_severity"].astype(str).str.strip().ne("")
            & self.ticket_df["true_category"].astype(str).str.strip().ne("")
        ]
        for _, row in labeled_rows.iterrows():
            pred = self.triage_ticket(row.to_dict())
            severity_correct = pred["severity"] == str(row.get("true_severity", "")).strip()
            category_correct = pred["category"] == str(row.get("true_category", "")).strip()
            correct_severity += severity_correct
            correct_category += category_correct
            if severity_correct and category_correct:
                correct += 1

        avg_time = 0.0
        return {
            "total_tickets": len(self.ticket_df),
            "open_tickets": open_tickets,
            "triage_accuracy": round(correct / len(labeled_rows) * 100, 2) if len(labeled_rows) else 0.0,
            "severity_accuracy": round(correct_severity / len(labeled_rows) * 100, 2) if len(labeled_rows) else 0.0,
            "category_accuracy": round(correct_category / len(labeled_rows) * 100, 2) if len(labeled_rows) else 0.0,
            "avg_time_to_draft_sec": avg_time,
            "tickets_processed": total,
        }

    def _load_documents(self) -> List[Dict[str, str]]:
        chunks: List[Dict[str, str]] = []
        for path in sorted(self.docs_dir.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            paragraphs = re.split(r"\n\s*\n+", text.strip())
            for paragraph in paragraphs:
                cleaned = re.sub(r"\s+", " ", paragraph).strip()
                if len(cleaned) < 80:
                    continue
                if cleaned.lower().startswith("## related documents"):
                    continue
                chunks.append({"source": path.name, "text": cleaned})
        return chunks

    def _build_vector_index(self):
        if not self.doc_chunks:
            return faiss.IndexFlatL2(1), np.empty((0, 1), dtype="float32")

        texts = [chunk["text"] for chunk in self.doc_chunks]
        embeddings = self.model.encode(texts, convert_to_numpy=True, normalize_embeddings=True)
        embeddings = np.asarray(embeddings, dtype="float32")
        index = faiss.IndexFlatL2(embeddings.shape[1])
        index.add(embeddings)
        return index, embeddings

    def retrieve_context(self, query: str, k: int = 3) -> List[Dict[str, str]]:
        if not self.doc_chunks:
            return []
        query_vector = self.model.encode([query], convert_to_numpy=True, normalize_embeddings=True)
        search_k = min(max(k * 3, k), len(self.doc_chunks))
        _, indices = self.index.search(np.asarray(query_vector, dtype="float32"), search_k)
        results: List[Dict[str, str]] = []
        seen_sources = set()
        for idx in indices[0]:
            if idx < 0 or idx >= len(self.doc_chunks):
                continue
            chunk = self.doc_chunks[int(idx)]
            if chunk["source"] in seen_sources:
                continue
            seen_sources.add(chunk["source"])
            results.append(dict(chunk))
            if len(results) == k:
                break
        return results

    def _extract_json(self, text: str) -> Optional[Dict[str, str]]:
        if not text:
            return None
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = text[start : end + 1]
            try:
                parsed = json.loads(candidate)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        return None

    def triage_ticket(self, ticket: Dict[str, Any]) -> Dict[str, str]:
        description = str(ticket.get("description", "")).strip()
        cache_key = (str(ticket.get("ticket_id", "")), description)
        cached_result = self._triage_cache.get(cache_key)
        if cached_result is not None:
            return dict(cached_result)
        if not description:
            result = {"severity": "Medium", "category": "Process Deviation"}
            self._triage_cache[cache_key] = result
            return dict(result)

        prompt = (
            "You are a quality triage assistant. Classify the incident into a severity and category. "
            "Return JSON only with keys 'severity' and 'category'. "
            "Allowed severities: High, Medium, Low. "
            "Allowed categories: Contamination, Equipment Calibration, Documentation Error, "
            "Process Deviation, Supplier Quality, Testing Failure, Packaging Defect, "
            "Non-Conforming Material.\n\nTicket: "
            f"{description}"
        )
        raw = self.llm.generate(prompt, max_new_tokens=80)
        parsed = self._extract_json(raw)
        if parsed:
            severity = str(parsed.get("severity", "")).strip().title()
            category = self._normalize_category(str(parsed.get("category", "")))
            if severity in ALLOWED_SEVERITIES and category in ALLOWED_CATEGORIES:
                result = {"severity": severity, "category": category}
                self._triage_cache[cache_key] = result
                return dict(result)

        description_lower = description.lower()
        category = self._infer_category(description_lower)
        severity = self._infer_severity(description_lower)
        result = {"severity": severity, "category": category}
        self._triage_cache[cache_key] = result
        return dict(result)

    def _normalize_category(self, category: str) -> str:
        normalized = re.sub(r"\s+", " ", category.strip()).lower()
        return CATEGORY_ALIASES.get(normalized, category.strip())

    def _infer_category(self, description: str) -> str:
        specific_rules = [
            ("Supplier Quality", ["supplier", "incoming inspection", "incoming silicon"]),
            ("Non-Conforming Material", ["non-conforming", "quarantined", "lot disposition", "hold tag", "accidental release"]),
            ("Packaging Defect", ["packaging", "carton", "labeling error", "esd shielding bag"]),
            ("Documentation Error", ["training record", "revision history", "deviation approval", "document", "work instruction"]),
            ("Equipment Calibration", ["preventive maintenance", "calibration", "sensor", "inconsistent readings"]),
            ("Contamination", ["foreign material", "embedded in wafer", "contamination", "particle"]),
        ]
        for category, keywords in specific_rules:
            if any(keyword in description for keyword in keywords):
                return category

        rules = {
            "Contamination": ["contamination", "particle", "metal", "cleanroom", "airborne", "surface defect", "slurry"],
            "Equipment Calibration": ["calibration", "drift", "tool", "furnace", "monitor", "temperature excursion", "dose monitor"],
            "Documentation Error": ["documentation", "work instruction", "traveler", "change order", "revision", "document"],
            "Process Deviation": ["deviation", "recipe", "operator skipped", "bypass", "incorrect", "moisture sensitivity", "step"],
            "Supplier Quality": ["supplier", "certificate", "incoming", "lot receiving", "vendor"],
            "Testing Failure": ["test", "burn-in", "pass/fail", "retested", "final test", "measurement inconsistency"],
            "Packaging Defect": ["packaging", "seal", "bag", "label", "carton"],
            "Non-Conforming Material": ["non-conforming", "quarantine", "scrap", "release", "material tag", "customer return"],
        }
        for category, keywords in rules.items():
            if any(keyword in description for keyword in keywords):
                return category
        return "Process Deviation"

    def _infer_severity(self, description: str) -> str:
        high_phrases = [
            "customer field return",
            "unauthorized parameter change",
            "units released without check",
            "not flagged by automated",
            "entire shift",
        ]
        low_phrases = [
            "labeling error",
            "outdated revision",
            "change order",
            "preventive maintenance on",
            "training record",
            "revision history",
            "carton drop test failure",
        ]
        medium_phrases = [
            "failed calibration check",
            "missing calibration records",
            "improperly stored",
        ]
        if any(phrase in description for phrase in high_phrases):
            return "High"
        if any(phrase in description for phrase in low_phrases):
            return "Low"
        if any(phrase in description for phrase in medium_phrases):
            return "Medium"

        high_keywords = ["failed", "scrap", "customer return", "quarantine", "non-conforming", "critical", "contamination", "excursion", "yield drop", "rejected", "particle"]
        medium_keywords = ["drift", "out of spec", "inconsistent", "potential", "deviation", "defect", "sampled"]
        low_keywords = ["documentation", "label error", "outdated", "minor", "revise"]
        if any(keyword in description for keyword in high_keywords):
            return "High"
        if any(keyword in description for keyword in medium_keywords):
            return "Medium"
        if any(keyword in description for keyword in low_keywords):
            return "Low"
        return "Medium"

    def draft_capa(self, ticket: Dict[str, Any], triage: Dict[str, str], context: List[Dict[str, str]]) -> str:
        ticket_id = ticket.get("ticket_id", "UNKNOWN")
        product_line = ticket.get("product_line", "General")
        severity = triage["severity"]
        category = triage["category"]
        relevant_sources = sorted({item["source"] for item in context})
        source_text = ", ".join(relevant_sources) if relevant_sources else "Quality SOP library"
        description = str(ticket.get("description", "")).strip()

        prompt = (
            "You are a quality engineer writing a CAPA recommendation. "
            "Use the incident description and the retrieved SOP references. "
            "Write a concise but actionable CAPA plan in plain English. "
            "Return a professional recommendation only, no markdown code fence.\n\n"
            f"Ticket ID: {ticket_id}\nProduct line: {product_line}\nSeverity: {severity}\nCategory: {category}\n"
            f"Description: {description}\n\nRelevant SOP references: {source_text}\n\n"
            "Draft a CAPA containing: containment, root cause investigation, corrective actions, preventive actions, and effectiveness verification."
        )
        raw = self.llm.generate(prompt, max_new_tokens=300)
        if raw:
            cleaned = raw.strip().replace("```", "").strip()
            if cleaned:
                return cleaned

        containment = "Quarantine affected material and place a temporary hold on any downstream use or shipment until containment is validated."
        if "non-conforming" in description.lower():
            containment = "Quarantine the affected lot and block release to the next process step until Quality dispositions the material."

        preventive = "Review the process controls, operator training, and monitoring checkpoints for similar lots to reduce recurrence."
        if category == "Equipment Calibration":
            preventive = "Add a preventive calibration check, increase monitoring frequency, and verify tool drift before the next production run."
        elif category == "Documentation Error":
            preventive = "Audit the document control workflow and reinforce revision control as part of line-side change management."
        elif category == "Contamination":
            preventive = "Strengthen cleanroom housekeeping, filter verification, and material handling controls in the affected area."
        elif category == "Supplier Quality":
            preventive = "Expand incoming inspection sampling and supplier quality review for the affected material and lot history."

        return (
            f"CAPA Recommendation for {ticket_id}\n"
            f"Product line: {product_line}\n"
            f"Severity: {severity} | Category: {category}\n\n"
            f"1. Containment\n{containment}\n\n"
            f"2. Root cause investigation\nReview the incident details against the relevant process and quality controls. Confirm whether the failure mode is tied to equipment drift, documentation control, process execution, or material handling.\n\n"
            f"3. Corrective actions\n- Open a CAPA record within two business days and assign an owner.\n- Implement immediate correction for the affected line, lot, or work instruction.\n- Verify the disposition, traceability, and affected units before release.\n\n"
            f"4. Preventive actions\n{preventive}\n\n"
            f"5. Effectiveness verification\nMonitor the defect rate, audit the correction, and verify that at least 30 days of stable process performance are achieved before closure.\n\n"
            f"6. Relevant SOPs\n{source_text}\n\n"
            f"7. Recommendation\nProceed with a formal CAPA review and ensure sign-off from Quality and the Process Owner before closure."
        )

    def process_ticket(self, ticket: Dict[str, Any]) -> Dict[str, Any]:
        start = time.perf_counter()
        triage = self.triage_ticket(ticket)
        context = self.retrieve_context(str(ticket.get("description", "")))
        draft = self.draft_capa(ticket, triage, context)
        elapsed = time.perf_counter() - start
        row_status = str(ticket.get("status", "pending")).strip().lower()
        state_status = row_status if row_status in {"pending", "approved", "rejected", "resolved", "awaiting_review"} else "pending"
        state: TicketState = {
            "ticket": ticket,
            "severity": triage["severity"],
            "category": triage["category"],
            "retrieved_context": [{"source": item["source"], "text": item["text"]} for item in context],
            "draft": draft,
            "status": state_status,
            "review_decision": "pending",
            "final_output": "",
            "time_to_draft_sec": round(elapsed, 3),
        }
        return state

    def finalize_review(self, state: Dict[str, Any], decision: str, edited_draft: Optional[str] = None) -> Dict[str, Any]:
        state["review_decision"] = decision
        if edited_draft is not None:
            state["draft"] = edited_draft
        if decision.lower() == "approve":
            state["status"] = "resolved"
            state["final_output"] = state["draft"]
        elif decision.lower() == "reject":
            state["status"] = "rejected"
            state["final_output"] = "Ticket returned to triage for rework."
        else:
            state["status"] = "awaiting_review"
        return state

    def _build_graph(self):
        workflow = StateGraph(TicketState)
        workflow.add_node(
            "triage",
            lambda state: {
                **state,
                "severity": self.triage_ticket(state["ticket"])["severity"],
                "category": self.triage_ticket(state["ticket"])["category"],
            },
        )
        workflow.add_node(
            "retrieve",
            lambda state: {
                **state,
                "retrieved_context": self.retrieve_context(str(state["ticket"].get("description", ""))),
            },
        )
        workflow.add_node(
            "draft",
            lambda state: {
                **state,
                "draft": self.draft_capa(
                    state["ticket"],
                    {"severity": state.get("severity", "Medium"), "category": state.get("category", "Process Deviation")},
                    state.get("retrieved_context", []),
                ),
            },
        )
        workflow.add_node("human_review", lambda state: {**state, "status": "awaiting_review", "review_decision": "pending"})
        workflow.add_node("finalize", lambda state: {**state, "status": "resolved", "final_output": state.get("draft", "")})

        workflow.add_edge(START, "triage")
        workflow.add_edge("triage", "retrieve")
        workflow.add_edge("retrieve", "draft")
        workflow.add_edge("draft", "human_review")
        workflow.add_conditional_edges(
            "human_review",
            lambda state: "finalize" if state.get("review_decision", "pending") == "approve" else "draft",
            {"finalize": "finalize", "draft": "draft"},
        )
        workflow.add_edge("finalize", END)
        return workflow.compile()

    def run_graph(self, ticket: Dict[str, Any], decision: Optional[str] = None) -> Dict[str, Any]:
        initial_state: TicketState = {"ticket": ticket, "review_decision": decision or "pending"}
        output = self.graph.invoke(initial_state)
        if decision:
            output = self.finalize_review(output, decision)
        return output


def build_sample_queue() -> List[Dict[str, Any]]:
    workflow = QualityWorkflow()
    tickets = [row.to_dict() for _, row in workflow.ticket_df.head(10).iterrows()]
    processed = []
    for ticket in tickets:
        processed.append(workflow.process_ticket(ticket))
    return processed


if __name__ == "__main__":
    workflow = QualityWorkflow()
    sample_ticket = workflow.ticket_df.iloc[0].to_dict()
    sample_state = workflow.process_ticket(sample_ticket)
    print(json.dumps({
        "ticket_id": sample_state["ticket"]["ticket_id"],
        "severity": sample_state["severity"],
        "category": sample_state["category"],
        "status": sample_state["status"],
        "preview": sample_state["draft"][:250],
    }, indent=2))
