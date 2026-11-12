"use client";

import Link from "next/link";
import { type FormEvent, useEffect, useRef, useState } from "react";
import { Button, Card, Field, Notice, PageHead, WorkingText } from "@/components/ui";
import { api } from "@/lib/api";
import { useAction, useApi, useMe } from "@/lib/hooks";
import { kr } from "@/lib/format";
import { useI18n, type TranslationKey } from "@/lib/i18n";
import type { Proposal } from "@/lib/types";

interface Message {
  id: number;
  role: "user" | "assistant";
  content: string;
  engine: string;
  proposals?: Proposal[] | null;
}

const MODES = ["auto", "pantry", "mealie", "direct", "diet", "waste"] as const;
const SUGGESTIONS: TranslationKey[] = [
  "assistant.suggest.1",
  "assistant.suggest.2",
  "assistant.suggest.3",
  "assistant.suggest.4",
  "assistant.suggest.5",
  "assistant.suggest.6",
];

function Offer({ proposal, busy, onAnswer }: { proposal: Proposal; busy: boolean; onAnswer: (approve: boolean) => void }) {
  const { t, locale } = useI18n();
  const text =
    proposal.type === "add_to_shopping_list"
      ? t("assistant.offer.shopping", { items: (proposal.items ?? []).join(", ") })
      : proposal.type === "create_price_alert"
        ? t("assistant.offer.alert", { query: proposal.query ?? "", price: kr(proposal.max_price ?? 0, locale) })
        : proposal.budget
          ? t("assistant.offer.planBudget", { budget: kr(proposal.budget, locale) })
          : t("assistant.offer.plan");
  return (
    <div className="offer">
      <span>{text}</span>
      {proposal.status === "open" ? (
        <div className="row">
          <Button size="sm" variant="primary" disabled={busy} onClick={() => onAnswer(true)}>
            {t("assistant.offer.approve")}
          </Button>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => onAnswer(false)}>
            {t("assistant.offer.decline")}
          </Button>
        </div>
      ) : (
        <span className="small muted">
          {proposal.status === "done" ? t("assistant.offer.done") : t("assistant.offer.declined")}
          {proposal.status === "done" && proposal.type === "make_plan" && (
            <>
              {" · "}
              <Link href="/plan">{t("assistant.offer.openPlan")}</Link>
            </>
          )}
          {proposal.status === "done" && proposal.type === "add_to_shopping_list" && (
            <>
              {" · "}
              <Link href="/shopping">{t("assistant.offer.openList")}</Link>
            </>
          )}
        </span>
      )}
    </div>
  );
}

export default function AssistantPage() {
  const { t } = useI18n();
  const { data: history, setData: setHistory, reload } = useApi<Message[]>("/assistant/history");
  const { busy, run } = useAction();
  const [draft, setDraft] = useState("");
  const me = useMe();
  const [mode, setMode] = useState<(typeof MODES)[number]>("auto");
  const [note, setNote] = useState<string | null>(null);
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => {
    end.current?.scrollIntoView({ block: "nearest" });
  }, [history, busy]);

  const ask = (message: string) =>
    run(async () => {
      setDraft("");
      setNote(null);
      // Shown right away, so the question does not vanish while the answer is on its way.
      setHistory([...(history ?? []), { id: -Date.now(), role: "user", content: message, engine: "ai" }]);
      const result = await api<{ fallback_reason: string | null }>("/assistant", { body: { message, mode } });
      if (result.fallback_reason) setNote(t(`assistant.localNote.${result.fallback_reason}` as TranslationKey));
      await reload();
    });

  const answer = (messageId: number, index: number, approve: boolean) =>
    run(async () => {
      await api(`/assistant/messages/${messageId}/proposals/${index}`, { body: { approve } });
      await reload();
    });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (draft.trim()) void ask(draft.trim());
  };

  return (
    <>
      <PageHead
        title={t("assistant.title")}
        subtitle={t("assistant.subtitle")}
        actions={
          history && history.length > 0 ? (
            <Button
              variant="ghost"
              onClick={() =>
                void run(async () => {
                  await api("/assistant/history", { method: "DELETE" });
                  await reload();
                })
              }
            >
              {t("assistant.clear")}
            </Button>
          ) : undefined
        }
      />
      <Card>
        {history && history.length === 0 && (
          <div className="tags">
            {SUGGESTIONS.map((key) => (
              <Button size="sm" key={key} disabled={busy} onClick={() => void ask(t(key))}>
                {t(key)}
              </Button>
            ))}
          </div>
        )}
        <div className="chat" aria-live="polite">
          {history?.map((message) => (
            <div className={`bubble ${message.role}`} key={message.id}>
              {message.content}
              {message.role === "assistant" && <span className="bubble-meta">{t(`assistant.engine.${message.engine}` as TranslationKey)}</span>}
              {message.proposals?.map((proposal, index) => (
                <Offer key={index} proposal={proposal} busy={busy} onAnswer={(approve) => void answer(message.id, index, approve)} />
              ))}
            </div>
          ))}
          {busy && (
            <div className="bubble assistant muted">
              <WorkingText texts={[t("working.assistant1"), t("working.assistant2"), t("working.longer")]} />
            </div>
          )}
          <div ref={end} />
        </div>
        {note && <Notice>{note}</Notice>}
        <form className="stack-sm" onSubmit={submit}>
          <div className="row" style={{ alignItems: "flex-end" }}>
            <div className="grow" style={{ flexBasis: 260 }}>
              <textarea
                rows={2}
                aria-label={t("assistant.placeholder")}
                placeholder={t("assistant.placeholder")}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) submit(e);
                }}
              />
            </div>
            <Button type="submit" variant="primary" working={busy} workingTexts={[t("working.assistantButton")]} disabled={!draft.trim()}>
              {t("assistant.send")}
            </Button>
          </div>
          <div style={{ maxWidth: 260 }}>
            <Field label={t("assistant.mode")}>
              <select value={mode} onChange={(e) => setMode(e.target.value as typeof mode)}>
                {MODES.filter((value) => value !== "mealie" || me.setup.mealie).map((value) => (
                  <option key={value} value={value}>
                    {t(`assistant.mode.${value}` as TranslationKey)}
                  </option>
                ))}
              </select>
            </Field>
          </div>
        </form>
      </Card>
    </>
  );
}
