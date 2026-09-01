import { useEffect, useMemo, useRef } from "react";
import { useNavigate } from "react-router-dom";

import {
  SEARCHABLE_CASES,
  SEARCHABLE_ENTITIES,
  SEARCHABLE_EVIDENCE,
} from "@/shared/mock/search-index";

interface PaletteResult {
  id: string;
  label: string;
  meta: string;
  group: "Case" | "Evidence" | "Entity" | "Action";
  icon: string;
  run: () => void;
}

interface CommandPaletteProps {
  open: boolean;
  query: string;
  onQueryChange: (query: string) => void;
  onClose: () => void;
  /** Case shortcuts ("Review AI findings", "Open entity graph", ...) target this case. */
  currentCaseId: string;
}

/**
 * Global ⌘K command palette (frontend-architecture.md §29, §37). Search is a client-side filter
 * over a small fixed index here because this is mock data (shared/mock/search-index.ts stands in
 * for the cross-resource search endpoint) — §29's "search is always server-side" rule applies once
 * that endpoint exists; this is not the pattern to copy for a real, unbounded dataset.
 */
export function CommandPalette({
  open,
  query,
  onQueryChange,
  onClose,
  currentCaseId,
}: CommandPaletteProps) {
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      inputRef.current?.focus();
    }
  }, [open]);

  const results = useMemo<PaletteResult[]>(() => {
    const go = (path: string) => () => {
      void navigate(path);
      onClose();
    };
    const all: PaletteResult[] = [
      ...SEARCHABLE_CASES.map((c) => ({
        id: `case-${c.id}`,
        label: c.label,
        meta: c.meta,
        group: "Case" as const,
        icon: "ph-folder",
        run: go(`/cases/${c.id}`),
      })),
      ...SEARCHABLE_EVIDENCE.map((e) => ({
        id: `evidence-${e.id}`,
        label: e.label,
        meta: e.meta,
        group: "Evidence" as const,
        icon: "ph-archive",
        run: go(`/cases/${e.caseId}/evidence`),
      })),
      ...SEARCHABLE_ENTITIES.map((e) => ({
        id: `entity-${e.id}`,
        label: e.label,
        meta: e.meta,
        group: "Entity" as const,
        icon: "ph-globe",
        run: go(`/cases/${e.caseId}/graph?pivot=${encodeURIComponent(e.id)}`),
      })),
      {
        id: "action-findings",
        label: "Review AI findings",
        meta: "Open the findings tab",
        group: "Action",
        icon: "ph-sparkle",
        run: go(`/cases/${currentCaseId}/findings`),
      },
      {
        id: "action-graph",
        label: "Open entity graph",
        meta: "Depth-bounded traversal",
        group: "Action",
        icon: "ph-graph",
        run: go(`/cases/${currentCaseId}/graph`),
      },
      {
        id: "action-report",
        label: "Draft incident report",
        meta: `${currentCaseId} · reports`,
        group: "Action",
        icon: "ph-file-text",
        run: go(`/cases/${currentCaseId}/reports`),
      },
      {
        id: "action-ingest",
        label: "Ingest evidence",
        meta: "Upload with hash verification",
        group: "Action",
        icon: "ph-upload-simple",
        run: go(`/cases/${currentCaseId}/evidence`),
      },
    ];
    const q = query.trim().toLowerCase();
    if (!q) return all.slice(0, 6);
    return all.filter((r) => `${r.label} ${r.meta} ${r.group}`.toLowerCase().includes(q));
  }, [query, currentCaseId, navigate, onClose]);

  if (!open) return null;

  return (
    <div
      className="dialog-backdrop"
      style={{ alignItems: "flex-start", justifyContent: "center", paddingTop: 88, zIndex: 50 }}
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-label="Command palette"
        className="dialog"
        onClick={(e) => {
          e.stopPropagation();
        }}
        style={{ width: "min(560px, 92vw)", padding: 0, overflow: "hidden" }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8.4,
            padding: "11.2px 16.8px",
            boxShadow: "inset 0 -1px 0 var(--sc-divider)",
          }}
        >
          <i
            className="ph ph-magnifying-glass"
            style={{ fontSize: 16, color: "var(--sc-accent)" }}
          />
          <input
            ref={inputRef}
            className="input"
            placeholder="Jump to a case, evidence id, entity or action…"
            value={query}
            onChange={(e) => {
              onQueryChange(e.target.value);
            }}
            style={{
              border: 0,
              boxShadow: "none",
              background: "transparent",
              padding: 0,
              minHeight: 26,
            }}
          />
          <span className="sc-code sc-text-muted">esc</span>
        </div>
        <div style={{ maxHeight: 360, overflowY: "auto", padding: 8.4 }}>
          {results.map((r) => (
            <button
              key={r.id}
              type="button"
              className="sc-palette-result"
              onClick={r.run}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 11.2,
                width: "100%",
                textAlign: "left",
                padding: "8.4px 11.2px",
                borderRadius: 8,
                border: 0,
                background: "transparent",
                color: "inherit",
                font: "inherit",
                cursor: "pointer",
              }}
            >
              <i
                className={`ph ${r.icon}`}
                style={{ fontSize: 16, color: "var(--sc-accent-300)" }}
              />
              <span style={{ display: "flex", flexDirection: "column", gap: 1, minWidth: 0 }}>
                <span style={{ fontSize: 13 }}>{r.label}</span>
                <span className="sc-text-muted" style={{ fontSize: 11 }}>
                  {r.meta}
                </span>
              </span>
              <span className="tag tag-neutral" style={{ marginLeft: "auto" }}>
                {r.group}
              </span>
            </button>
          ))}
          {results.length === 0 && (
            <p
              className="sc-text-muted"
              style={{ fontSize: 13, padding: "16.8px 11.2px", margin: 0 }}
            >
              No matches. Search is scoped to cases you are a member of.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
