"""Graph tests with a stubbed LLM (no API key needed): python test_intellidoc.py"""
import json
import sqlite3
import tempfile
from pathlib import Path

import intellidoc as I

tmp = Path(tempfile.mkdtemp())
I.DATA_DIR = tmp / "data"
app = I.build_app()
calls = {"n": 0}


def doc(name, text):
    p = tmp / name
    p.write_text(text)
    return str(p)


def fake_ok(schema, prompt, attempts=3):
    calls["n"] += 1
    if schema is I.Classification:
        return I.Classification(doc_type="resume" if "Skills" in prompt else "invoice")
    if schema is I.Resume:
        return I.Resume(name="Asha", skills=["python"])
    return I.Invoice(vendor="Acme", invoice_number="1", invoice_date="2026-09-14", total_amount=525.0)


# 1. invoice -> integrated
I.ask = fake_ok
r = I.run_file(app, doc("inv.txt", "Invoice total 525"))
assert r["status"] == "integrated" and r["doc_type"] == "invoice" and r["retries"] == 0, r
assert r["extracted_data"]["total_amount"] == 525.0

# 2. routing by type: resume goes to hr_system
r = I.run_file(app, doc("cv.txt", "Skills: python"))
assert r["status"] == "integrated" and r["doc_type"] == "resume", r
rows = sqlite3.connect(I.DATA_DIR / "intellidoc.db").execute("SELECT system FROM records").fetchall()
assert sorted(x[0] for x in rows) == ["erp", "hr_system"], rows

# 3. validation: negative total is rejected -> retries -> escalated to review queue
def fake_bad(schema, prompt, attempts=3):
    if schema is I.Classification:
        return I.Classification(doc_type="invoice")
    return I.Invoice.model_construct(vendor="A", invoice_number="1",
                                     invoice_date="2026-09-14", total_amount=-5.0, currency="USD")
I.ask = fake_bad
r = I.run_file(app, doc("bad.txt", "bad invoice"))
assert r["status"] == "failed" and r["retries"] == 2 and r["extracted_data"] is None, r
q = [json.loads(x) for x in (I.DATA_DIR / "review_queue.jsonl").read_text().splitlines()]
assert len(q) == 1 and q[0]["doc_type"] == "invoice" and q[0]["errors"], q

# 4. transient failure then success: retry recovers
state = {"fail": 1}
def fake_flaky(schema, prompt, attempts=3):
    if schema is not I.Classification and state["fail"]:
        state["fail"] -= 1
        raise RuntimeError("boom")
    return fake_ok(schema, prompt)
I.ask = fake_flaky
r = I.run_file(app, doc("flaky.txt", "Invoice"))
assert r["status"] == "integrated" and r["retries"] == 1, r

# 5. monitoring stream was written
ev = (I.DATA_DIR / "events.jsonl").read_text().splitlines()
assert len(ev) >= 15 and json.loads(ev[0])["node"] == "ingest"
print("ALL 5 TESTS PASSED")
