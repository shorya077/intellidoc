"""IntelliDoc - LangGraph architecture powered by Gemini (Google API key)."""
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Optional, TypedDict

from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, field_validator

load_dotenv(Path(__file__).with_name(".env"))
MAX_RETRIES = 1
DATA_DIR = Path(os.getenv("INTELLIDOC_DATA", "data"))

# Models tried in order; override with INTELLIDOC_MODELS=a,b,c in .env
MODELS = [m.strip() for m in os.getenv(
    "INTELLIDOC_MODELS",
    "gemini-3.5-flash,gemini-3.8-flash,gemini-3.1-flash-lite,"
    "gemini-3.7-flash,gemini-flash-latest",
).split(",") if m.strip()]
TRANSIENT = ("500", "502", "504", "INTERNAL", "503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "timed out", "timeout")


def ask(schema, prompt, attempts=3):
    """Structured Gemini call: back off on overload, then fall back to the next model."""
    last = None
    for name in MODELS:
        model = ChatGoogleGenerativeAI(model=name, temperature=0, timeout=60, max_retries=0)
        for i in range(attempts):
            try:
                return model.with_structured_output(schema).invoke(prompt)
            except Exception as e:
                last, msg = e, str(e)
                if "404" in msg or "NOT_FOUND" in msg:
                    break  # model gone -> next model
                if any(t in msg for t in TRANSIENT):
                    time.sleep(2 ** i)  # 1s, 2s, 4s
                    continue
                raise  # real error (bad key, bad schema...) -> surface it
        print(f"[llm] {name} unavailable, trying next model", flush=True)
    raise last


# ---------------------------------------------------------------- State
class IntelliDocState(TypedDict):
    doc: dict
    doc_type: Optional[str]
    metadata: dict
    extracted_data: Optional[dict]
    status: Literal["pending", "processing", "integrated", "failed"]
    errors: list[str]
    retries: int


# ------------------------------------------- Document-type schemas (registry)
class Invoice(BaseModel):
    vendor: str
    invoice_number: str
    invoice_date: str = Field(description="YYYY-MM-DD")
    total_amount: float
    currency: str = "USD"

    @field_validator("total_amount")
    @classmethod
    def _positive(cls, v):
        if v <= 0:
            raise ValueError("total_amount must be > 0")
        return v

    @field_validator("invoice_date")
    @classmethod
    def _iso_date(cls, v):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ValueError("invoice_date must be YYYY-MM-DD")
        return v


class Contract(BaseModel):
    parties: list[str]
    effective_date: str = Field(description="YYYY-MM-DD")
    term: Optional[str] = None
    summary: str


class Resume(BaseModel):
    name: str
    email: Optional[str] = None
    skills: list[str]
    years_experience: Optional[float] = None


class Other(BaseModel):
    summary: str


SCHEMAS: dict[str, type[BaseModel]] = {
    "invoice": Invoice, "contract": Contract, "resume": Resume, "other": Other,
}


class Classification(BaseModel):
    doc_type: Literal["invoice", "contract", "resume", "other"]


# ---------------------------------------------------------------- Nodes
def ingest_node(state: IntelliDocState) -> dict:
    doc = state["doc"]
    if "text" not in doc:
        path = Path(doc["path"])
        if path.suffix.lower() == ".pdf":
            from pypdf import PdfReader
            text = "\n".join(p.extract_text() or "" for p in PdfReader(path).pages)
        else:
            text = path.read_text(encoding="utf-8", errors="ignore")
        doc = {"text": text, "source": str(path)}
    meta = {
        "source": doc.get("source", "inline"),
        "chars": len(doc["text"]),
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }
    return {"doc": doc, "metadata": meta, "status": "processing"}


def classify_node(state: IntelliDocState) -> dict:
    try:
        result = ask(
            Classification,
            "Classify this document as invoice, contract, resume, or other.\n\n"
            + state["doc"]["text"][:6000],
        )
        return {"doc_type": result.doc_type}
    except Exception as e:
        return {"doc_type": None, "errors": state["errors"] + [f"classify: {e}"]}


def process_node(state: IntelliDocState) -> dict:
    schema = SCHEMAS[state["doc_type"]]
    try:
        result = ask(
            schema,
            f"Extract the fields from this {state['doc_type']}. "
            "Use null when a value is absent; do not invent data.\n\n"
            + state["doc"]["text"][:12000],
        )
        return {"extracted_data": schema.model_validate(result.model_dump()).model_dump()}
    except Exception as e:
        return {"extracted_data": None, "errors": state["errors"] + [f"process: {e}"]}


def handle_error_node(state: IntelliDocState) -> dict:
    retries = state["retries"] + 1
    update = {"retries": retries}
    if retries > MAX_RETRIES:
        update["status"] = "failed"  # escalate for manual review
    return update


def _db_adapter(system: str) -> Callable[[dict, dict], None]:
    """Writes to a SQLite table per target system. Replace with a real API call."""
    def adapter(data: dict, meta: dict) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(DATA_DIR / "intellidoc.db") as db:
            db.execute("CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY, "
                       "system TEXT, source TEXT, data TEXT, created_at TEXT)")
            db.execute("INSERT INTO records (system, source, data, created_at) "
                       "VALUES (?, ?, ?, ?)",
                       (system, meta.get("source"), json.dumps(data),
                        datetime.now(timezone.utc).isoformat()))
    return adapter


ADAPTERS: dict[str, Callable[[dict, dict], None]] = {
    "invoice": _db_adapter("erp"),
    "contract": _db_adapter("contract_mgmt"),
    "resume": _db_adapter("hr_system"),
    "other": _db_adapter("archive"),
}


def human_review_node(state: IntelliDocState) -> dict:
    """Escalation target: queue the document for a person to look at."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_DIR / "review_queue.jsonl", "a") as f:
        f.write(json.dumps({
            "queued_at": datetime.now(timezone.utc).isoformat(),
            "source": state["metadata"].get("source"),
            "doc_type": state["doc_type"],
            "errors": state["errors"],
            "preview": state["doc"]["text"][:300],
        }) + "\n")
    return {"status": "failed"}


def integrate_node(state: IntelliDocState) -> dict:
    ADAPTERS[state["doc_type"]](state["extracted_data"], state["metadata"])
    return {"status": "integrated"}


# ---------------------------------------------------------- Routing
def route_by_type(state: IntelliDocState) -> str:
    return "process" if state["doc_type"] in SCHEMAS else "error"


def route_after_process(state: IntelliDocState) -> str:
    return "ok" if state["extracted_data"] is not None else "error"


def route_after_error(state: IntelliDocState) -> str:
    if state["retries"] > MAX_RETRIES:
        return "escalate"
    return "retry_classify" if state["doc_type"] is None else "retry_process"


# ------------------------------------------------------------ Assembly
def build_app():
    g = StateGraph(IntelliDocState)
    g.add_node("ingest", ingest_node)
    g.add_node("classify", classify_node)
    g.add_node("process", process_node)
    g.add_node("handle_error", handle_error_node)
    g.add_node("integrate", integrate_node)
    g.add_node("human_review", human_review_node)

    g.add_edge(START, "ingest")
    g.add_edge("ingest", "classify")
    g.add_conditional_edges("classify", route_by_type,
                            {"process": "process", "error": "handle_error"})
    g.add_conditional_edges("process", route_after_process,
                            {"ok": "integrate", "error": "handle_error"})
    g.add_conditional_edges("handle_error", route_after_error, {
        "retry_classify": "classify",
        "retry_process": "process",
        "escalate": "human_review",
    })
    g.add_edge("human_review", END)
    g.add_edge("integrate", END)
    return g.compile()


# ------------------------------------------------------------- Monitoring
def publish(node: str, state: dict) -> None:
    """Dashboard hook: append a state snapshot after each node."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    snap = {"ts": datetime.now(timezone.utc).isoformat(), "node": node,
            "source": state.get("metadata", {}).get("source"),
            "status": state["status"], "doc_type": state["doc_type"],
            "retries": state["retries"], "errors": state["errors"][-1:]}
    with open(DATA_DIR / "events.jsonl", "a") as f:
        f.write(json.dumps(snap) + "\n")
    print(f"[monitor] {node:<13} status={state['status']} type={state['doc_type']} "
          f"retries={state['retries']}", flush=True)


def run_file(app, path: str) -> dict:
    state: dict = {"doc": {"path": path}, "doc_type": None, "metadata": {},
                   "extracted_data": None, "status": "pending", "errors": [], "retries": 0}
    for event in app.stream(state, stream_mode="updates"):
        for node, update in event.items():
            state.update(update)
            publish(node, state)
    return state


def collect(paths: list[str]) -> list[str]:
    files = []
    for p in paths:
        pp = Path(p)
        files += sorted(str(f) for f in pp.iterdir()
                        if f.suffix.lower() in (".txt", ".pdf")) if pp.is_dir() else [p]
    return files


if __name__ == "__main__":
    if not os.getenv("GOOGLE_API_KEY"):
        sys.exit("Set GOOGLE_API_KEY in .env or your environment.")
    if len(sys.argv) < 2:
        sys.exit("Usage: python intellidoc.py <file-or-folder> [more files...]")

    app = build_app()
    for f in collect(sys.argv[1:]):
        print(f"\n=== {f}")
        r = run_file(app, f)
        print(json.dumps({k: r[k] for k in ("status", "doc_type", "extracted_data", "errors")},
                         indent=2))
