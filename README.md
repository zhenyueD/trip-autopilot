# Trip Autopilot

**An AI-native travel agent.** Give it a destination you know nothing about. It researches the trip, verifies the facts that can ruin it, and hands you a handbook you can actually travel with — then revises it from a single sentence, and shows its work.

Built solo by a product manager orchestrating AI coding agents (a human-on-the-loop workflow: the PM sets acceptance standards and verifies evidence; agents write the code). This repository is both the product and the process record.

## The problem with AI travel planning today

Chatbots write plausible itineraries with invented prices, last year's opening hours, and buses that stopped running. The failure mode isn't bad prose — it's **one wrong fact that sends you on a wasted trip**: a closed museum, a missed last boat, a pass that costs more than the fares it replaces.

Trip Autopilot is designed around that failure mode.

## How it works

**1. Single-session agent, verified inline.**
One agent session drafts the handbook, and checks *on the spot* — while writing — only the facts that will be printed and can hurt you: prices, last trains, opening days, seasonal closures. Every checked fact enters an **evidence ledger** with its verbatim source quote, URL, and check date. Facts that aren't checked are labelled, never silently asserted.

**2. Errors are graded, not averaged.**
- **S1 — trip-ruining** (closed venue, wrong date, non-existent booking, wrong last train): hard gate, must be sourced.
- **S2 — adjustment-level** (an unpriced estimate, a time buffer): shipped, labelled.
- **S3 — imperceptible** (a bus at 4:15 vs 4:20): trusted to the model, no machinery wasted.

**3. A completeness gate, not a hope.**
A draft cannot be marked complete unless it covers every day of the trip, every day has real content, and no placeholder survives. Validation errors name the exact field path and rule, so the agent can fix them instead of guessing. (This gate exists because an earlier version once "completed" an empty shell — the failure, the diagnosis, and the fix are all in the commit history and docs.)

**4. Revision is the product: the receipt.**
Say *"Day 2 is too rushed — slow it down."* You get back a **revision receipt**: what changed (a real diff), one line of judgment ("Day 2 had three timed commitments before noon; the boat now leaves after lunch instead"), the alternative that was considered, and one-tap actions. Judgment and facts are kept visibly separate.

**5. Two-layer handbook.**
The first-eye layer is at most 3 pages — why this design, a real map with the day-by-day table, the photos that make you want to go — because that's all a first reader will grant you. The full handbook (every sight: what to see, how to get there, how long, when it's best, where the trap is; transport down to boarding point and departure time) sits behind it for after the decision.

## What's in this repo

| Path | What it is |
|---|---|
| `core/packages-core.tar.gz.enc` | The engine, encrypted (see below): the single-session agent — drafting, tool-checked facts, evidence ledger, revision, completeness gate — plus the earlier research/verification pipeline |
| `apps/v1/` | The product frontend (conversation, progress, handbook, revision receipt) |
| `tools/` | Service entry points (API on :7392), PDF rendering |
| `tests/` | Test suites for the agent, API, document and presentation layers |
| `docs/` | Product contracts, battle reports, acceptance records |
| `samples/` | Real outputs: a West Lake winter handbook, a 9-day South of France handbook (3-page first-eye layer + full edition) |

### The core is encrypted — on purpose

The interface, contracts, acceptance records, tests and real outputs in this repo are open. The engine source itself is the author's working IP, so it ships as an AES-256-CBC (PBKDF2) encrypted archive: `core/packages-core.tar.gz.enc`. The passphrase is shared separately by the author with authorized reviewers and testers. With it:

```bash
openssl enc -d -aes-256-cbc -pbkdf2 -in core/packages-core.tar.gz.enc -out core.tar.gz
tar -xzf core.tar.gz   # restores packages/
```

## Run it

(First decrypt the core archive as shown above, so `packages/` exists.)

```bash
# backend (single-session agent service)
python tools/serve_single_session.py --port 7392 --static-root apps/v1

# create a trip
curl -X POST localhost:7392/api/agent/trips \
  -d '{"destination":"Hangzhou West Lake","start_date":"2026-12-12","end_date":"2026-12-13","travelers":2,"pace":"relaxed"}'
# → 202 {trip, token}; poll GET /api/agent/trips/<id> for the handbook + PDF
# revise with one sentence:
curl -X POST localhost:7392/api/agent/trips/<id>/message -d '{"message":"Day 2 is too rushed, slow it down"}'
```

Requires a DeepSeek API key (`DEEPSEEK_API_KEY`) for live generation.

## Design principles (learned the hard way)

- **The harness only keeps the books.** Verbatim-quote ledgers, arithmetic checks, state records. All semantic judgment belongs to the model — every rule that tried to out-judge the model eventually blocked good output.
- **Verification is inline, not a review stage.** Facts arrive verified or labelled; nothing enters the printable set unverified.
- **Degrade per stage, never empty the run.** One failed lookup degrades one card, not the handbook.
- **Rent engines, own the acceptance.** Generation and rendering are commodities; the moat is the verified supply ledger, the user's travel memory, and a revision receipt people learn to trust.

## Status

Working MVP: end-to-end generation → verified handbook → PDF → one-sentence revision, proven live on real orders (see `samples/`). Built Nov 2026.
