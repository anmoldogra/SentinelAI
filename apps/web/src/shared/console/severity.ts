import type { Severity } from "@/shared/mock/types";

/** Shared severity → tag-class mapping so every screen renders the same visual language (§19). */
export function severityTagClass(severity: Severity): string {
  switch (severity) {
    case "Critical":
      return "tag tag-accent";
    case "High":
      return "tag tag-accent-2";
    case "Medium":
    case "Low":
      return "tag tag-neutral";
  }
}

const DOT_COLOR: Record<"ok" | "warn" | "idle", string> = {
  ok: "var(--sc-dot-ok)",
  warn: "var(--sc-dot-warn)",
  idle: "var(--sc-dot-idle)",
};

export function dotColor(status: "ok" | "warn" | "idle"): string {
  return DOT_COLOR[status];
}
