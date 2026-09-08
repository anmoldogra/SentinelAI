/**
 * In-memory access-token holder.
 *
 * `security-architecture.md` §35 and `frontend-architecture.md` §9 both require session state to
 * be **in-memory only, never persisted** — so this module deliberately has no `localStorage` or
 * `sessionStorage` path, and the ESLint config bans those globals outright so one cannot be added
 * by accident. A full page reload therefore loses the token and the app must re-authenticate;
 * that is the intended trade-off, not an oversight (a refresh-token flow via an HttpOnly cookie
 * is the sanctioned way to restore a session, and belongs to the auth increment).
 *
 * This is the token *store* only. The React auth context (§6) that owns login/logout and the
 * 401 redirect is a later increment; it will set and clear the token through here.
 */

let accessToken: string | null = null;

/**
 * Dev-only seam so local work is not blocked on the unbuilt login screen.
 *
 * Set `VITE_DEV_ACCESS_TOKEN` in `apps/web/.env.local` (gitignored) and it is used as the bearer
 * token for every request. Two properties make this safe:
 *   - `import.meta.env.DEV` is statically replaced by Vite, so this whole block is dead-code
 *     eliminated from a production build — it cannot ship.
 *   - The value still only ever lives in memory (§35); nothing is written to storage.
 *
 * **This does not manufacture a session.** The backend resolves a bearer token against a real
 * `platform.sessions` row (`platform/auth/dependencies.py`), so the token must be one the server
 * would actually accept. Mint one with `make dev-token` against a stack that has had
 * `make migrate && make create-admin` run; anything else is rejected as unauthenticated. The seam
 * exists because the login *screen* is unbuilt, not because the login *endpoint* is.
 */
if (import.meta.env.DEV) {
  const devToken = import.meta.env.VITE_DEV_ACCESS_TOKEN;
  if (typeof devToken === "string" && devToken.length > 0) {
    accessToken = devToken;
  }
}

/** The current bearer token, or `null` when unauthenticated. */
export function getAccessToken(): string | null {
  return accessToken;
}

/** Record the token for subsequent API calls. Called by the auth context on login. */
export function setAccessToken(token: string): void {
  accessToken = token;
}

/** Drop the token — on logout, or when the API client sees a 401. */
export function clearAccessToken(): void {
  accessToken = null;
}
