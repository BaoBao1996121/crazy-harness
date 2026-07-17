# Crazy A2A Control Room Design

## Design intent

This is an operations and learning surface, not a landing page. Its first job is to make causality legible: who woke up, what the model proposed, what the Kernel accepted, and which durable fact changed.

## Visual direction

- Tone: a quiet field-research atelier: comforting and lightly magical while remaining an operational instrument.
- Base: mist white and pale botanical green; accents use lake blue, moss green, wheat gold, and warm coral so the interface stays readable without becoming clinical.
- Inspiration boundary: evoke the calm nature, old-world craft, and gentle wonder associated with pastoral fantasy animation without copying characters, frames, or copyrighted artwork.
- Shape: 4-8px radii, precise borders, stable rows, no decorative floating cards or gradient orbs.
- Type: system sans for labels; monospace for cursors, ids, event types, and JSON.
- Surface: translucent paper-like panels, quiet dividers, and soft green-grey shadows replace the former black control-console treatment.
- Motion: short state transitions and a restrained live pulse; respect reduced-motion settings.

## Information architecture

```text
Top command bar: runtime / run status / create / fault controls
Left rail: resident agents + mailbox counts
Center: filtered causal timeline + subsystem tabs
Right inspector: selected event, causation, payload, trust boundary
```

Desktop uses three stable columns. Tablet moves the inspector below the timeline. Mobile becomes one column with a horizontal agent strip; controls wrap without overlay.

## Learning affordances

- Candidate and formal fact use visibly different badges.
- A causation link jumps to the parent event.
- Context view displays retained, discarded, offloaded, token estimate, and prompt hash.
- A2A view displays sender, receiver, depth, remaining budget, and policy decision.
- Dream/Memory view displays signal, frozen evidence, admission zone, and active status.
- Evolution view stops honestly at the latest real gate; pending Shadow is not rendered as success.
- Hidden chain-of-thought is never shown. Only structured plans, commands, evidence, and decisions appear.

## Success checks

- The full demo can be started with one command and watched without refreshing.
- A one-shot crash appears in the timeline and the same Delivery later recovers.
- No text, button, counter, timeline row, or inspector panel overlaps at 1440x900, 1024x768, or 390x844.
