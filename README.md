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

## Design notes

Zero runtime dependencies, deliberately: this ships into arbitrary customer
codebases, so nothing here should ever conflict with a design partner's own
dependency pins. stdlib only (`urllib.request` for HTTP, `threading` for the
buffer worker).
