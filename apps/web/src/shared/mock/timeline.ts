import type { TimelineEvent } from "./types";

/** Mock stand-in for the case timeline's aggregated event feed (frontend-architecture.md §28). */
export const TIMELINE_BY_CASE: Record<string, TimelineEvent[]> = {
  "CASE-2041": [
    {
      when: "26 Aug 19:41",
      what: "VPN login for m.hale from AS14061",
      detail: "First session from this ASN for the tenant · EV-0114",
      dot: "warn",
    },
    {
      when: "26 Aug 20:12",
      what: "First SMB write to the file cluster",
      detail: "Share NL-FS02\\ops · EV-0231",
      dot: "idle",
    },
    {
      when: "27 Aug 04:10",
      what: "Outbound transfer to 185.220.101.44 begins",
      detail: "3.1 GB over 40 minutes · EV-0301",
      dot: "warn",
    },
    {
      when: "27 Aug 04:52",
      what: "Encryption starts on NL-FS02",
      detail: "Ransom note written at 04:56 · EV-0298 · EV-0402",
      dot: "ok",
    },
    {
      when: "27 Aug 06:14",
      what: "Case opened, containment initiated",
      detail: "Cluster isolated by d.mensah",
      dot: "idle",
    },
    {
      when: "28 Aug 15:48",
      what: "OSINT match on ransom wallet",
      detail: "Paste dated 16 Aug · OSINT-0042",
      dot: "ok",
    },
  ],
};
