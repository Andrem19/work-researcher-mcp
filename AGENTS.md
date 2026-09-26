# Agent instructions — Remtz Hub integration

These repository-local instructions supplement the repository README/docs. When work is performed for a Remtz Hub RH/ENG issue, Linear status is part of the deliverable.


## Mandatory Remtz Hub Linear lifecycle (ENG-266)

When this repository is worked on for a Remtz Hub RH/ENG issue, Linear status is part of the deliverable, not optional bookkeeping.

- On actual claim/start, move `Backlog`/`Todo` → `In Progress` in the same work cycle.
- After **every completed slice, PR, merge, or significant gate**, re-read acceptance and recalculate the issue status.
- Implementation complete but owner/device/Android/reboot/login/live acceptance remains → `In Review` with the exact pending gate.
- `Done` only after every acceptance criterion and required evidence is complete. Merge/commit/test success alone is never Done.
- Remove stale blocker labels when their condition is no longer true.
- After each work cycle reconcile the parent RH project too: once real work has started it must not remain Backlog/Planned; project completion requires project-level acceptance.
- Before stop/handoff reconcile issue status + project status + blocker labels. If Linear is unavailable, persist the intended transition through the Hub reporting/outbox path.
- When uncertain between `Done` and `In Review`, use `In Review` and state what remains.

Hub ENG-266 automates this contract; until then every agent must execute it manually.
