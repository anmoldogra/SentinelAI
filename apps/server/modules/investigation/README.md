# modules/investigation

Core AI orchestration.

## Purpose

The only module designed to read across domain boundaries. Consumes evidence and case data from the other modules (via their public interfaces/events, never their internal storage) to perform cross-domain correlation, generate hypotheses, and propose investigative leads — always attributed back to source evidence for analyst review. See `docs/architecture.md` for why this module is the one exception to strict domain isolation, and `docs/vision.md` for the "analyst-in-the-loop" principle it must uphold.

## Extraction path

The exception to the general recipe: because it depends on every other module's public interface, it is naturally the **last** module worth extracting, and its extraction looks different — once extracted it must call out to every other extracted module's now-network API, exactly mirroring what it already does in-process today. Nothing about its own logic changes; only how many of its dependencies are still in-process vs. networked at the time it's extracted.

## Status

Placeholder. No code yet — see `docs/roadmap.md` Phase 3.
