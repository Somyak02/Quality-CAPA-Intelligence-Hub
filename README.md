# Quality CAPA Review Dashboard

A local Streamlit application for triaging quality incident tickets, retrieving relevant quality SOP excerpts, drafting CAPA recommendations, and recording human review decisions.

## Features

- Classifies each incident by severity and quality category.
- Retrieves relevant excerpts from the local SOP library with FAISS embeddings.
- Generates a CAPA draft using a local text-to-text model when available.
- Falls back to deterministic triage rules and a deterministic CAPA template when model generation is unavailable.
- Supports approval and rejection workflows.
- Allows users to enter a new incident description in the UI.
- Generates a unique ticket ID for new incidents and persists records to CSV and Excel.
- Mirrors the ticket CSV to `input_docs/incident_tickets.csv` for convenient access.
- Provides the generated CAPA as a downloadable Word document.
- Displays severity accuracy and category accuracy across all tickets.
- Filters the ticket queue by approval status and severity.
- Deduplicates retrieved SOP context by source and excludes cross-reference-only sections.

## Project Structure

```text
.
├── app.py                         # Streamlit dashboard
├── quality_workflow.py            # Retrieval, triage, CAPA, metrics, and persistence logic
├── generate_synthetic_data.py     # Recreates the 40-ticket sample dataset
├── test.py                        # Basic end-to-end smoke script
├── requirements.txt               # Python dependencies
└── input_docs/
    └── quality_agent_project_data/
        ├── incident_tickets.csv   # Ticket data and persisted statuses
        └── quality_sops/           # Markdown SOP source documents
```

## Requirements

- Python 3.10 or newer recommended
- A virtual environment
- Internet access the first time the embedding and text-generation models are downloaded
- Enough local disk and memory for `BAAI/bge-base-en-v1.5` and `google/flan-t5-base`

## Setup

From the project root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell blocks activation for the current process, run:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
```

## Run the Dashboard

```powershell
streamlit run app.py
```

Streamlit prints a local URL, normally `http://localhost:8501`.

The dashboard workflow is:

1. Load tickets and the SOP library.
2. Triage tickets by severity and category.
3. Retrieve up to three unique SOP sources for the selected incident.
4. Generate or fall back to a CAPA draft.
5. Review, edit, approve, or reject the draft.
6. Persist the ticket and status to `incident_tickets.csv` and `incident_tickets.xlsx`.
7. Download the generated or approved CAPA as a Word document.

## Generate Sample Data

The repository includes a 40-ticket synthetic dataset. To recreate it:

```powershell
python generate_synthetic_data.py
```

This overwrites `input_docs/quality_agent_project_data/incident_tickets.csv` and resets ticket statuses because the generator does not include status values. Use it only when you intend to reset the sample data.

## Run the Smoke Script

```powershell
python test.py
```

The script loads the workflow, processes the first ticket, prints a triage result and CAPA preview, and prints metrics. It may download the local models on first run.

## Metrics

The dashboard reports accuracy across the full ticket dataset, independent of the queue status filter:

- **Severity accuracy:** percentage of tickets with the correct severity.
- **Category accuracy:** percentage of tickets with the correct category.

The status filter changes which tickets appear in the queue and the open-review count, but it does not change these accuracy metrics.

The metric labels are compared against `true_severity` and `true_category` in the CSV. The synthetic dataset currently contains 40 labeled tickets.

## Classification Behavior

`QualityWorkflow.triage_ticket()` follows this sequence:

1. Reuse a cached result for the same ticket ID and description.
2. Ask the local FLAN-T5 model for JSON severity and category output.
3. Normalize category aliases and reject labels outside the allowed sets.
4. Use specific phrase rules before broad keyword rules when model output is missing or invalid.
5. Return a deterministic default for an empty description.

The deterministic rules are tuned to the supplied synthetic quality dataset. For production use, replace or extend them with reviewed labeled examples and a formal evaluation set.

## Retrieval Behavior

SOP files are split into paragraph-sized chunks and embedded with `BAAI/bge-base-en-v1.5`. FAISS performs nearest-neighbor search for each ticket description. Retrieval searches extra candidates, removes duplicate SOP sources, and returns at most three unique SOP excerpts. `Related Documents` sections are excluded from the index because they create repetitive cross-reference results.

## Status Persistence

Ticket statuses are stored in the CSV `status` column as lowercase values such as:

- `approved`
- `rejected`
- empty or `None` for pending work

The workflow explicitly uses an assignment-compatible pandas dtype before updating the status column. Streamlit resource caching is versioned in `app.py` so workflow changes can invalidate stale cached instances.

## Troubleshooting

### Model download or loading errors

The application catches local model-loading failures and uses deterministic fallbacks. Confirm that the environment can access the model repositories and has sufficient disk space if you need model-generated output.

### Status assignment dtype error

Restart or refresh Streamlit after code changes. The workflow normalizes the status column dtype before assigning text values, and the app cache version forces a fresh workflow instance.

### Accuracy displays zero

Accuracy is evaluated over the full ticket dataset. Check that the CSV contains `true_severity`, `true_category`, `description`, and `ticket_id` columns, then refresh Streamlit so the current workflow cache is loaded.

### Repeated SOP excerpts

Refresh the application after retrieval changes. The current implementation excludes `Related Documents` chunks and limits results to one excerpt per SOP source.

## Development Notes

- `app.py` owns Streamlit state, filters, queue rendering, and review actions.
- `quality_workflow.py` owns data loading, retrieval, triage, CAPA drafting, metrics, status persistence, and the LangGraph workflow.
- The application writes status changes to the source CSV, so keep a backup if the data is important.
- Do not commit model caches, virtual environments, or production ticket data to source control.
