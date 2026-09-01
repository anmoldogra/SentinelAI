import type { CopilotMessage } from "./types";

/** Seed transcript + canned reply for the investigation copilot panel (mock only — no LLM call). */
export const COPILOT_SEED_BY_CASE: Record<string, CopilotMessage[]> = {
  "CASE-2041": [
    {
      id: "seed-1",
      who: "Copilot",
      text: "Three evidence clusters correlate on the same VPN session id. The wallet address in the ransom note also appears in an OSINT paste captured 11 days before the intrusion.",
      cite: "EV-0114 · EV-0231 · OSINT-0042",
    },
    { id: "seed-2", who: "r.okafor", text: "Which host was encrypted first?" },
    {
      id: "seed-3",
      who: "Copilot",
      text: "NL-FS02 at 27 Aug 04:52, four minutes after the last outbound transfer to 185.220.101.44 completed.",
      cite: "EV-0298 · EV-0301",
    },
  ],
};

export const COPILOT_SUGGESTED_PROMPTS = [
  "What contradicts the exfiltration hypothesis?",
  "List evidence not yet linked to any finding",
  "Draft the containment section of the report",
];

export const COPILOT_CANNED_REPLY =
  "Pulled the matching artifacts and ranked them by proximity to the encryption event. Two are already linked to F-0031; the third is unreviewed.";
export const COPILOT_CANNED_CITE = "EV-0114 · EV-0298 · EV-0377";
