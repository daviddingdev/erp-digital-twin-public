# Code

The orchestration layer of the monthly cycle — copied verbatim from the private repo, with
personal and client identifiers replaced by stable pseudonyms (`ClientCo`, `PlantA`,
`PlantB`, `ERP-A`). The mapping is applied before the leak scan, so a name it missed would
withhold the file rather than publish it half-masked.

**What is deliberately absent:** the two extraction scripts, `snapshot.py` and `delta.py`.
They carry 36 ERP-vendor schema identifiers — table and column names — and masking those
would fictionalise the schema the code is written against. A reader who knows the product
recognises its tables no matter what the strings say, so the honest choice is to withhold
them and describe what they do in the [README](../README.md) instead.

What is here is the part worth reading anyway: not the queries, but everything that
decides whether the month actually landed.

| File | Lines | What it demonstrates |
|---|---:|---|
| [`run_cycle.py`](run_cycle.py) | 232 | **The cycle as a state machine.** The month stays `pending` until verification passes; a daily retry is a no-op on a healthy month and re-attempts on a broken one, so a late upstream refresh resolves itself without anyone watching. Success and failure go to different channels, failure at high priority with the attempt count. Written after a month where every script exited 0 and produced a byte-identical snapshot that nobody noticed for five days. |
| [`verify_cycle.py`](verify_cycle.py) | 104 | **Assertions about the outcome, not the steps.** Is each entity's snapshot actually fresh, does it carry zero section errors, does the matching delta report exist, is its plumbing check clean, were the generated views rebuilt against the new cutoff, do the servers answer. Deterministic, no model involved — the whole point is that it cannot be talked into a yes. |
| [`status.py`](status.py) | 112 | Exposes the last verification result as machine-readable state, so the landing page and the notifier read the same answer rather than each deciding for themselves. |
| [`wiki_ctl.py`](wiki_ctl.py) | 241 | Control surface for the generated wiki: rebuild, serve, and report freshness. |
| [`notify.py`](notify.py) | 131 | The push layer — one channel for results, another for failures, and a permanent local record because the push service only keeps hours. |

_Read `verify_cycle.py` first. It is the shortest file here and the reason the pipeline is
trustworthy: everything else can be wrong as long as this refuses to say it was fine._
