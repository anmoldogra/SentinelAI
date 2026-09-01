import type { EntityFact, GraphRelation } from "./types";

/** Mock stand-in for `GET /cases/{id}/graph` (frontend-architecture.md §27, api-design.md §6). */
export const ENTITY_KINDS_BY_CASE: Record<string, Record<string, EntityFact>> = {
  "CASE-2041": {
    "m.hale": {
      kind: "Account",
      note: "VPN account belonging to a contractor; the credential used for initial access.",
    },
    "NL-FS02": {
      kind: "Host",
      note: "File cluster node; first host encrypted and the source of the outbound transfer.",
    },
    "185.220.101.44": {
      kind: "IP address",
      note: "Exfiltration destination, first observed for this tenant during the incident.",
    },
    "bc1q…7f4e": { kind: "Wallet", note: "Bitcoin address named in the ransom note." },
    "ASHEN-DRIFT": {
      kind: "Threat actor",
      note: "Affiliate crew tracked across two intel feeds.",
    },
    AS14061: { kind: "ASN", note: "Hosting ASN for the egress destination." },
    "svc-backup": {
      kind: "Account",
      note: "Service account seen on the same subnet; unproven lead.",
    },
    "note.readme.txt": {
      kind: "Artifact",
      note: "Ransom note written after encryption completed.",
    },
    "vpn-gw-01": { kind: "Host", note: "VPN concentrator that logged the anomalous session." },
  },
};

export const GRAPH_RELATIONS_BY_CASE: Record<string, GraphRelation[]> = {
  "CASE-2041": [
    { from: "m.hale", to: "vpn-gw-01", type: "authenticated_to" },
    { from: "m.hale", to: "NL-FS02", type: "accessed" },
    { from: "m.hale", to: "svc-backup", type: "same_subnet" },
    { from: "NL-FS02", to: "185.220.101.44", type: "transferred_to" },
    { from: "NL-FS02", to: "note.readme.txt", type: "contains" },
    { from: "185.220.101.44", to: "AS14061", type: "announced_by" },
    { from: "185.220.101.44", to: "ASHEN-DRIFT", type: "attributed_to" },
    { from: "bc1q…7f4e", to: "ASHEN-DRIFT", type: "controlled_by" },
    { from: "bc1q…7f4e", to: "note.readme.txt", type: "referenced_in" },
  ],
};

export const DEFAULT_PIVOT_BY_CASE: Record<string, string> = {
  "CASE-2041": "m.hale",
};
