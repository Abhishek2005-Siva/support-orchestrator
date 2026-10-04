"""Locust load test. Start the API in mock mode first:
    LLM_MODE=mock RATE_LIMIT_PER_MIN=100000 uvicorn app.main:app --port 8000
    locust -f loadtests/locustfile.py --host http://localhost:8000 --users 100 --spawn-rate 20
"""
import json, random
from pathlib import Path
from locust import HttpUser, between, task
ROOT = Path(__file__).resolve().parents[1]
CREDS = json.loads((ROOT / "data/demo_credentials.json").read_text())
MSGS = [json.loads(l)["message"] for l in (ROOT / "evals/golden.jsonl").read_text().splitlines()]
CUSTOMERS = [k for k in CREDS if k.startswith("CUST-")]

class Customer(HttpUser):
    wait_time = between(0.1, 1.0)
    def on_start(self):
        cid = random.choice(CUSTOMERS)
        r = self.client.post("/auth/token", json={"client_id": cid, "client_secret": CREDS[cid]})
        self.headers = {"Authorization": "Bearer " + r.json()["access_token"]}
    @task(10)
    def ask(self): self.client.post("/v1/query", json={"message": random.choice(MSGS)}, headers=self.headers, name="/v1/query")
    @task(1)
    def health(self): self.client.get("/healthz", name="/healthz")
