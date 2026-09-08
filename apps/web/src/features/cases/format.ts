/**
 * Presentation formatting for case timestamps.
 *
 * The API returns ISO-8601 UTC strings; these render them in the viewer's locale and zone. Both
 * fall back to the raw string rather than showing "Invalid Date" — a timestamp the browser cannot
 * parse is still more useful to an investigator than a blank.
 */

/** Date only — for dense contexts like the dashboard table. */
export function formatDate(iso: string): string {
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? iso : parsed.toLocaleDateString();
}

/** Date and time — for detail views, where the precise moment matters to a custody timeline. */
export function formatDateTime(iso: string): string {
  const parsed = new Date(iso);
  return Number.isNaN(parsed.getTime()) ? iso : parsed.toLocaleString();
}
