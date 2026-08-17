# Radar: daily digest of AI tooling changes

Autonomous pipeline that collects changes across a tracked AI stack once a day,
deduplicates, enriches facts from primary sources, marks each signal against a
persistent corpus, and publishes to Telegram.

Built for the "Agents VS Chat" webinar demo. The core is headless: the pipeline
ends by writing `Signal` records to storage, and every surface reads from there.

## Documents

- `docs/PRD.md` — product requirements, v1.2
- `docs/AGENT-NOTES.md` — implementation notes for the executing agent
- `docs/directors-review.md` — board review: consensus, disagreements, cut list

## Status

Bootstrapping. Nothing runs yet.
