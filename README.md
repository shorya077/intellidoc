# IntelliDoc - LangGraph Document Workflow

An agentic document-processing pipeline built with **LangGraph** and the **Google Gemini API**.
It ingests a document (TXT or text-based PDF), classifies it, extracts and validates structured
fields, and integrates the result into the right enterprise system - with error handling,
retries, human escalation and live monitoring.

The design follows the architecture in [`docs/IntelliDoc_LangGraph_Architecture.docx`](docs/IntelliDoc_LangGraph_Architecture.docx).

## Architecture

```
START -> ingest -> classify -> (route_by_type) -> process -> (route_after_process) -> integrate -> END
                                    |                              |
                                    +-------> handle_error <-------+
                                                   |
                                     retry --------+-------- escalate -> human_review -> END
```

| Node | Responsibility |
|---|---|
| `ingest_node` | Normalizes input (TXT/PDF) into a common document + metadata |
| `classify_node` | Gemini tags the type: invoice, contract, resume, other |
| `route_by_type` | Conditional edge: continue or send to error handling |
| `process_node` | Gemini extracts fields into a Pydantic schema, then validates them |
| `handle_error_node` | Counts retries; escalates after the limit |
| `integrate_node` | Dispatches to the adapter for the document type (SQLite stand-ins) |
| `human_review_node` | Queues escalated documents in `data/review_queue.jsonl` |

Design principles from the doc, as implemented:
- Every node is a plain function: state in, partial update out.
- Routing lives in separate conditional-edge functions, not inside nodes.
- New document type = one entry in `SCHEMAS` and `ADAPTERS`.
- Error handling is its own node with a retry loop.
- State is streamed after each node to `data/events.jsonl` (the monitoring hook), shown by `dashboard.py`.

### Differences from the original design doc
- `route_by_type` is wired after `classify` (the doc defines it but goes straight to `process`).
- The retry route is split into `retry_classify` / `retry_process`.
- Added a `human_review` node for escalated documents (the doc's suggested extension).
- LLM calls use per-call backoff and model fallback, since hosted Gemini models return temporary 503/500 errors.

## Setup

```bash
git clone <this-repo-url> && cd intellidoc
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then put your GOOGLE_API_KEY in .env
```

## Run

```bash
python test_intellidoc.py                 # graph logic tests, no API key needed
python intellidoc.py samples/             # process a file or a whole folder
python intellidoc.py pdf_samples/         # PDFs
python dashboard.py                       # live dashboard at http://localhost:8000
```

Output goes to `data/`: `intellidoc.db` (SQLite records), `events.jsonl` (monitoring stream),
`review_queue.jsonl` (escalated documents).

## Example result

```json
{
  "status": "integrated",
  "doc_type": "invoice",
  "extracted_data": {
    "vendor": "Northwind Traders Pvt. Ltd.",
    "invoice_number": "NW-7783",
    "invoice_date": "2026-08-21",
    "total_amount": 28910.0,
    "currency": "INR"
  },
  "errors": []
}
```

## Tests

`test_intellidoc.py` stubs the LLM and checks: happy path, routing by type, validation failure
leading to escalation and the review queue, retry recovery, and the monitoring stream.

## Limitations
- Scanned PDFs / images need OCR (not yet implemented).
- Adapters write to SQLite; replace them with real ERP/HR/contract API calls.
