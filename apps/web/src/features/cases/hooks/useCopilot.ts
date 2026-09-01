import { useCallback, useEffect, useRef, useState } from "react";

import {
  COPILOT_CANNED_CITE,
  COPILOT_CANNED_REPLY,
  COPILOT_SEED_BY_CASE,
} from "@/shared/mock/copilot";
import type { CopilotMessage } from "@/shared/mock/types";

const REPLY_LATENCY_MS = 900;

export interface Copilot {
  messages: CopilotMessage[];
  draft: string;
  setDraft: (draft: string) => void;
  send: (text: string) => void;
}

/** Investigation copilot panel state (mock — no LLM call), scoped to the open case. */
export function useCopilot(caseId: string): Copilot {
  const [messages, setMessages] = useState<CopilotMessage[]>(
    () => COPILOT_SEED_BY_CASE[caseId] ?? [],
  );
  const [draft, setDraft] = useState("");
  const nextId = useRef(0);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    setMessages(COPILOT_SEED_BY_CASE[caseId] ?? []);
    setDraft("");
    if (timeoutRef.current !== null) {
      clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
  }, [caseId]);

  useEffect(
    () => () => {
      if (timeoutRef.current !== null) {
        clearTimeout(timeoutRef.current);
      }
    },
    [],
  );

  const send = useCallback((text: string) => {
    const trimmed = text.trim();
    if (!trimmed) return;
    nextId.current += 1;
    const userMessage: CopilotMessage = {
      id: `local-${String(nextId.current)}`,
      who: "r.okafor",
      text: trimmed,
    };
    setMessages((prev) => [...prev, userMessage]);
    setDraft("");
    timeoutRef.current = setTimeout(() => {
      nextId.current += 1;
      const reply: CopilotMessage = {
        id: `local-${String(nextId.current)}`,
        who: "Copilot",
        text: COPILOT_CANNED_REPLY,
        cite: COPILOT_CANNED_CITE,
      };
      setMessages((prev) => [...prev, reply]);
    }, REPLY_LATENCY_MS);
  }, []);

  return { messages, draft, setDraft, send };
}
