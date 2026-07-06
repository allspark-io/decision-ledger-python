# decision-ledger-python

Python SDK for [decision-ledger](https://github.com/allspark-io/decision-ledger)
(Layer 1) — the FR-1.3(a) native SDK on-ramp. See
[allspark-io/architecture](https://github.com/allspark-io/architecture) for
the full spec and ADRs.

API docs: https://allspark-io.github.io/decision-ledger-python/

## Install

```bash
pip install allspark-decision-ledger --index-url https://allspark-014548221675.d.codeartifact.eu-west-1.amazonaws.com/pypi/sdks/simple/
```

(That index needs an AWS SigV4 auth token — see `aws codeartifact login`.
Not yet published to public PyPI.)

## Usage

```python
from allspark_io import DecisionLedgerClient

client = DecisionLedgerClient(
    "http://app.decision-ledger.svc.cluster.local:8080",
    deployment_id="my-deployment", agent_id="my-agent",
)

check = client.check(mandate_ref="my-mandate", mandate_version="1", transaction={...})
client.record_decision(
    principal="...", mandate_ref="my-mandate", mandate_version="1",
    episode_id="...", transaction={...}, execution={...}, unchecked=check.unchecked,
)
```

Package distribution name is `allspark-decision-ledger` (what you `pip
install`); the importable module is `allspark_io` — same pattern as e.g.
`pip install python-dateutil` → `import dateutil`.

## Framework integrations

Wiring the client into an agent framework by hand means rewriting the same
three things every time: hook registration, unwrapping the framework's tool
result format, and the check-then-record sequence. Only one part is
actually yours — mapping a specific tool's result into a `transaction` dict.

For [Strands Agents](https://strandsagents.com):

```python
from allspark_io import DecisionLedgerClient
from allspark_io.integrations.strands import decision_ledger_hook

client = DecisionLedgerClient(url, deployment_id="...", agent_id="...")

def build_transaction(result, tool_input, state):
    return {
        "counterparty": result.get("carrier"),
        "instrument": f"lane:{result.get('origin')}-{result.get('destination')}",
        "quantity": 1,
        "price": result.get("price"),
        "currency": result.get("currency"),
    }

DecisionLedgerHooks = decision_ledger_hook(
    client, tool_name="book_carrier", mandate_ref="...", mandate_version="1",
    build_transaction=build_transaction,
    # optional: build_external_anchors=lambda result: {"booking_ref": result["booking_ref"]}
)

agent = Agent(..., hooks=[DecisionLedgerHooks(), ...])
```

`client=None` (e.g. the ledger URL isn't configured) is a valid, harmless
no-op — no need to conditionally build the hooks list.

`allspark_io.integrations.strands` lazily imports `strands-agents` (only
when you import this specific module) — the core package stays
dependency-free regardless.

## Design notes

Zero runtime dependencies, deliberately: this ships into arbitrary customer
codebases, so nothing here should ever conflict with a design partner's own
dependency pins. stdlib only (`urllib.request` for HTTP, `threading` for the
buffer worker).
