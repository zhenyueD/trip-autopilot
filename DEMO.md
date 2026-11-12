# Demo script (≈5 minutes)

For a live walkthrough of Trip Autopilot. Everything below runs locally; the only external dependency is the model API key for *new* generations — all review steps work on pre-generated trips with no waiting.

## Setup (before the meeting)

On the demo Mac, double-click **TripDemo.command** on the Desktop (or run `~/start-trip-demo.sh`): it health-checks the 7392 service, starts it if needed — with the main repo's `apps/v1` as static root, which is where the single-session UI (`session.html`) lives — and opens the UI in Chrome:

```
http://127.0.0.1:7392/session.html
```

**Step 0 — the day before (rehearsal):** in the same Chrome profile you will present from, create the West Lake trip from the intake form and let it finish. The session persists in the browser's local storage, so on interview day the page restores the finished handbook immediately — no generation wait in the room. Do not clear site data in between.

Pre-loaded sample trips:
- **Hangzhou West Lake, 2 days, winter** — generated live by the current build (generation → handbook → PDF, end to end); the finished v4 PDF is in `samples/` as the no-network fallback.
- **South of France, 9 days** — the quality benchmark: 3-page first-eye layer over a 23-page verified handbook (`samples/`).

## The walkthrough

**1. The first-eye layer (30s).** Open the West Lake handbook. Three pages: why this design, the map + day table, the photos. Point: *a first-time reader gives you three pages, not twenty-three — the product is shaped around that.*

**2. The handbook is verified, not vibes (60s).** Scroll the deep layer. Every sight: what to see / how to get there / how long / when it's best / the trap. Open the verification record (§⑥): each printed price and last-train time traces to a verbatim source quote with a date. Point: *errors are graded — S1 facts that can ruin a trip are hard-gated; everything else is labelled, not hidden.*

**3. The signature moment: one-sentence revision (90s).** In the conversation, type: **"Day 2 is too rushed — slow it down."** Show the revision receipt: the diff, one line of judgment, the alternative considered, one-tap actions. Point: *this receipt is the product's trust surface — judgment and facts stay visibly separate, and the change is auditable.*

**4. Start a new trip live (optional, runs in background).** Enter an unfamiliar destination and submit. While it generates, talk about the architecture: single-session agent, inline verification, completeness gate. Point: *the gate exists because v1 once "completed" an empty shell — the failure report is in `docs/`.*

**5. Close on the workflow, not just the app (60s).** This repo was built by one PM orchestrating AI agents: written battle orders (goal / acceptance / boundaries), independent verification of every delivery, honest ledgers (unknowns are recorded as unknown, never zero). Point: *Human on the loop — the human sets standards and judges; agents execute; nothing ships on a claim.*

## If the network fails

` samples/` contains the finished handbooks as PDFs — the whole story (first-eye layer, verification record, corrections column) is readable from the South of France sample alone.
