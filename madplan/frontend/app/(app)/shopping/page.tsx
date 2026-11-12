"use client";

import { Fragment, useEffect, useRef, useState } from "react";
import { SavingsTile, StatTile } from "@/components/charts";
import { Button, Card, Empty, Field, Modal, Notice, NumberField, PageHead } from "@/components/ui";
import { api } from "@/lib/api";
import { kr, quantity } from "@/lib/format";
import { useAction, useApi, useToast } from "@/lib/hooks";
import { useI18n, type TranslationKey } from "@/lib/i18n";
import { flushTicks, isOffline, queuedTicks, queueTick } from "@/lib/offline";
import type { ShoppingItem, ShoppingListData } from "@/lib/types";

// The order most Danish supermarkets are walked in, as the backend's catalog has it.
const SECTION_ORDER = ["produce", "bakery", "meat", "fish", "dairy", "dry", "frozen", "drinks", "snacks", "household", "other"];
// While a list is open, what others tick off shows up here this often.
const LIVE_SECONDS = 5;

function bySection(items: ShoppingItem[]): ShoppingItem[] {
  return [...items].sort((a, b) => SECTION_ORDER.indexOf(a.section) - SECTION_ORDER.indexOf(b.section) || a.name.localeCompare(b.name, "da"));
}

interface ListSummary {
  id: number;
  name: string;
  plan_id: number | null;
  completed_at: string | null;
  total: number | null;
  savings: number | null;
}

export default function ShoppingPage() {
  const { t, locale } = useI18n();
  const { show } = useToast();
  const { data: lists, reload: reloadLists } = useApi<ListSummary[]>("/shopping-lists");
  const [selected, setSelected] = useState<number | null>(null);
  const [shopping, setShopping] = useState<ShoppingListData | null>(null);
  const [draft, setDraft] = useState("");
  const [completing, setCompleting] = useState(false);
  const { busy, run } = useAction();

  useEffect(() => {
    const fromPlan = Number(new URLSearchParams(window.location.search).get("plan"));
    if (fromPlan && lists) {
      const match = lists.find((l) => l.plan_id === fromPlan);
      if (match) setSelected(match.id);
    }
  }, [lists]);

  const activeId = selected ?? lists?.find((l) => !l.completed_at)?.id ?? lists?.[0]?.id ?? null;

  useEffect(() => {
    if (activeId === null) return;
    void run(async () => setShopping(await api<ShoppingListData>(`/shopping-lists/${activeId}`)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  // Ticks made without a network go as soon as there is one, and then the list is fetched again.
  const [waiting, setWaiting] = useState(0);
  useEffect(() => {
    const send = async () => {
      if (Object.keys(queuedTicks()).length === 0) return;
      pending.current += 1;
      try {
        await flushTicks((id, checked) => api(`/shopping-items/${id}`, { method: "PATCH", body: { checked } }));
      } finally {
        pending.current -= 1;
      }
      setWaiting(Object.keys(queuedTicks()).length);
      if (activeId !== null) setShopping(await api<ShoppingListData>(`/shopping-lists/${activeId}`));
    };
    setWaiting(Object.keys(queuedTicks()).length);
    void send().catch(() => undefined);
    window.addEventListener("online", send);
    return () => window.removeEventListener("online", send);
  }, [activeId]);

  // Two people in the store, each with a phone: what one ticks off, the other sees within seconds.
  // Not while a tick of one's own is on its way, so it does not flicker back.
  const pending = useRef(0);
  const open = Boolean(shopping && !shopping.completed_at);
  useEffect(() => {
    if (activeId === null || !open) return;
    const timer = setInterval(() => {
      if (pending.current > 0 || document.visibilityState !== "visible" || !navigator.onLine) return;
      void api<ShoppingListData>(`/shopping-lists/${activeId}`)
        .then((fresh) => {
          if (pending.current === 0) setShopping(fresh);
        })
        .catch(() => undefined);
    }, LIVE_SECONDS * 1000);
    return () => clearInterval(timer);
  }, [activeId, open]);

  if (lists && lists.length === 0) {
    return (
      <>
        <PageHead title={t("shopping.title")} subtitle={t("shopping.subtitle")} />
        <Card>
          <Empty>{t("shopping.empty")}</Empty>
        </Card>
      </>
    );
  }

  // What was ticked offline wins over the kept copy until it has been sent.
  const queued = typeof window === "undefined" ? {} : queuedTicks();
  if (shopping && waiting > 0) {
    shopping.items.forEach((item) => {
      const tick = queued[String(item.id)];
      if (tick !== undefined) item.checked = tick;
    });
  }
  const summary = shopping?.summary;
  const buyable = shopping?.items.filter((i) => !i.covered_by_pantry) ?? [];
  const covered = shopping?.items.filter((i) => i.covered_by_pantry) ?? [];
  const groups = new Map<string, ShoppingItem[]>();
  for (const item of buyable) {
    const key = item.store ?? t("shopping.other");
    groups.set(key, [...(groups.get(key) ?? []), item]);
  }
  const left = buyable.filter((i) => !i.checked).reduce((sum, i) => sum + (i.cost ?? 0), 0);

  const toggle = (item: ShoppingItem, checked: boolean) => {
    setShopping((current) => current && { ...current, items: current.items.map((i) => (i.id === item.id ? { ...i, checked } : i)) });
    pending.current += 1;
    api(`/shopping-items/${item.id}`, { method: "PATCH", body: { checked } })
      .catch((caught) => {
        // No signal in the store: the tick stays, and goes when the network is back.
        if (isOffline(caught)) {
          queueTick(item.id, checked);
          setWaiting(Object.keys(queuedTicks()).length);
        } else {
          show(t("common.error"));
        }
      })
      .finally(() => {
        pending.current -= 1;
      });
  };

  const addItem = () =>
    run(async () => {
      const updated = await api<ShoppingListData>(`/shopping-lists/${activeId}/items`, { body: { name: draft.trim() } });
      setShopping(updated);
      if (updated.allergy_warning) show(t("shopping.allergyWarning", { name: draft.trim(), allergy: updated.allergy_warning }));
      setDraft("");
    });

  const packLabel = (item: ShoppingItem) => {
    if (!item.pack_quantity || !item.unit) return "";
    // Pieces sold singly need no explanation. Eggs by the ten, or two bags of potatoes, do.
    if (item.unit === "stk" && (item.pack_quantity === 1 || !Number.isInteger(item.pack_quantity))) return "";
    if (item.packs === 1 && item.quantity === item.pack_quantity) return "";
    return t("shopping.packOf", { count: item.packs, size: quantity(item.pack_quantity, item.unit, locale) });
  };

  const plan = summary?.store_plan;
  const single = summary?.best_single_store;

  return (
    <>
      <PageHead
        title={t("shopping.title")}
        subtitle={t("shopping.subtitle")}
        actions={
          shopping?.plan_id && !shopping.completed_at ? (
            <Button
              variant="ghost"
              disabled={busy}
              onClick={() => void run(async () => setShopping(await api<ShoppingListData>(`/plans/${shopping.plan_id}/refresh-list`, { method: "POST" })))}
            >
              {t("shopping.refresh")}
            </Button>
          ) : undefined
        }
      />

      {lists && lists.length > 1 && (
        <Field label={t("shopping.select")}>
          <select value={activeId ?? ""} onChange={(e) => setSelected(Number(e.target.value))}>
            {lists.map((l) => (
              <option key={l.id} value={l.id}>
                {l.name}
                {l.total != null ? ` · ${kr(l.total, locale)}` : ""}
                {l.completed_at ? ` · ${t("shopping.completed")}` : ""}
              </option>
            ))}
          </select>
        </Field>
      )}

      {summary && (
        <div className="grid-4">
          <StatTile label={t("plan.total")} value={kr(summary.total, locale)} sub={t("plan.priceQuality", { real: summary.real_prices, estimated: summary.estimated_prices, unknown: summary.unknown_prices })} />
          <SavingsTile summary={summary} />
          <StatTile label={t("shopping.remaining")} value={kr(left, locale)} />
        </div>
      )}

      {plan && plan.stores.length > 0 && (
        <Card title={t("stores.title")}>
          {summary && summary.extra_store_saves > 0 && plan.stores.length > 1 && single ? (
            <Notice kind="good">{t("stores.worthIt", { amount: kr(summary.extra_store_saves, locale), store: single.stores[0] })}</Notice>
          ) : (
            <p>{t("stores.single", { store: plan.stores.join(", ") })}</p>
          )}
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th />
                  <th className="num">{t("stores.goods")}</th>
                  <th className="num">{t("stores.travel")}</th>
                  <th className="num">{t("stores.extraStop")}</th>
                  <th className="num">{t("stores.total")}</th>
                </tr>
              </thead>
              <tbody>
                {summary?.store_alternatives.map((option, index) => (
                  <tr key={option.stores.join("+")}>
                    <td className={index === 0 ? "strong" : undefined}>{option.stores.join(" + ")}</td>
                    <td className="num">{kr(option.goods, locale)}</td>
                    <td className="num">{option.missing_distance && option.travel === 0 ? "–" : kr(option.travel, locale)}</td>
                    <td className="num">{kr(option.extra_stop_cost, locale)}</td>
                    <td className={`num ${index === 0 ? "strong" : ""}`}>{kr(option.total, locale)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {plan.missing_distance && <p className="small muted">{t("stores.distanceMissing")}</p>}
        </Card>
      )}

      {[...groups.entries()].map(([store, items]) => (
        <Card key={store} title={store} actions={<span className="num small muted">{kr(items.reduce((sum, i) => sum + (i.cost ?? 0), 0), locale)}</span>}>
          {store === t("shopping.other") && <p className="small muted">{t("shopping.otherHint")}</p>}
          <div>
            {bySection(items).map((item, index, sorted) => (
              <Fragment key={item.id}>
              {(index === 0 || sorted[index - 1].section !== item.section) && (
                <div className="section-head small strong muted">{t(`section.${item.section}` as TranslationKey)}</div>
              )}
              <div className={`list-row ${item.checked ? "done" : ""}`}>
                <label className="check grow">
                  <input type="checkbox" checked={item.checked} disabled={Boolean(shopping?.completed_at)} onChange={(e) => toggle(item, e.target.checked)} />
                  <span>
                    <span className="item-name">{item.name}</span>{" "}
                    <span className="small muted">
                      {quantity(item.quantity, item.unit, locale)}
                      {packLabel(item) ? ` · ${packLabel(item)}` : ""}
                    </span>
                    {item.offer_title && (
                      <small>
                        {item.offer_title}
                        {!shopping?.completed_at && (
                          <>
                            {" · "}
                            <button
                              type="button"
                              className="link-button"
                              title={t("shopping.wrongHint")}
                              onClick={(event) => {
                                event.preventDefault();
                                void run(async () => setShopping(await api<ShoppingListData>(`/shopping-items/${item.id}/wrong-match`, { method: "POST" })));
                              }}
                            >
                              {t("shopping.wrong")}
                            </button>
                          </>
                        )}
                      </small>
                    )}
                  </span>
                </label>
                <span className="right">
                  <span className="num strong">{item.manual ? "" : kr(item.cost, locale)}</span>
                  {item.saving > 0 && <span className="small delta-good"> −{kr(item.saving, locale)}</span>}
                  <br />
                  <span className={`badge ${item.price_source === "offer" ? "good" : ""}`}>
                    {item.manual ? t("shopping.manual") : t(`shopping.sourceLabel.${item.price_source}` as TranslationKey)}
                  </span>
                </span>
              </div>
              </Fragment>
            ))}
          </div>
        </Card>
      ))}
      {waiting > 0 && <Notice kind="warning">{t("shopping.offlineTicks", { count: waiting })}</Notice>}
      {open && <p className="small muted no-print">{t("shopping.live")}</p>}

      {shopping && !shopping.completed_at && (
        <Card>
          <form
            className="row"
            onSubmit={(e) => {
              e.preventDefault();
              if (draft.trim()) void addItem();
            }}
          >
            <input className="grow" style={{ flexBasis: 200 }} aria-label={t("shopping.add")} placeholder={t("shopping.addPlaceholder")} value={draft} onChange={(e) => setDraft(e.target.value)} />
            <Button type="submit" disabled={!draft.trim() || busy}>
              {t("shopping.add")}
            </Button>
          </form>
          <div>
            <Button variant="primary" onClick={() => setCompleting(true)}>
              {t("shopping.complete")}
            </Button>
          </div>
        </Card>
      )}

      {covered.length > 0 && (
        <Card title={t("shopping.covered")}>
          <div className="tags">
            {covered.map((item) => (
              <span className="badge" key={item.id}>
                {item.name}
              </span>
            ))}
          </div>
        </Card>
      )}

      {completing && shopping && (
        <CompleteDialog
          listId={shopping.id}
          items={shopping.items}
          onClose={() => setCompleting(false)}
          onDone={async () => {
            setCompleting(false);
            setShopping(await api<ShoppingListData>(`/shopping-lists/${shopping.id}`));
            await reloadLists();
          }}
        />
      )}
    </>
  );
}

interface ReceiptReading {
  store: string | null;
  total: number | null;
  lines_sum: number;
  adds_up: boolean;
  lines: { text: string; name: string; quantity: number | null; unit: string | null; price: number; item_id: number | null; item_name: string | null }[];
  not_on_receipt: { id: number; name: string }[];
}

function CompleteDialog({ listId, items, onClose, onDone }: { listId: number; items: ShoppingItem[]; onClose: () => void; onDone: () => Promise<void> }) {
  const { t, locale } = useI18n();
  const { busy, run } = useAction();
  const { busy: reading, run: runRead } = useAction();
  const fileInput = useRef<HTMLInputElement>(null);
  const [amount, setAmount] = useState<number | null>(null);
  const [receipt, setReceipt] = useState<ReceiptReading | null>(null);
  // Which lines of the reading to keep, and which list item each one is, as the person decides.
  const [keep, setKeep] = useState<boolean[]>([]);
  const [matches, setMatches] = useState<(number | null)[]>([]);

  const read = (files: FileList | null) => {
    if (!files || files.length === 0) return;
    const form = new FormData();
    Array.from(files).forEach((file) => form.append("photos", file));
    void runRead(async () => {
      const result = await api<ReceiptReading>(`/shopping-lists/${listId}/receipt`, { form });
      setReceipt(result);
      setKeep(result.lines.map(() => true));
      setMatches(result.lines.map((line) => line.item_id));
      if (result.total != null) setAmount(result.total);
    });
  };

  const confirmed = receipt
    ? receipt.lines
        .map((line, index) => ({ item_id: matches[index], name: line.name, quantity: line.quantity, unit: line.unit, price: line.price }))
        .filter((_, index) => keep[index])
    : null;

  return (
    <Modal title={t("shopping.completeTitle")} onClose={onClose}>
      <p className="small muted">{t("shopping.completeHint")}</p>
      <div className="stack-sm">
        <div className="row">
          <Button working={reading} workingTexts={[t("receipt.reading"), t("working.minute")]} onClick={() => fileInput.current?.click()}>
            {t("receipt.photo")}
          </Button>
          <input ref={fileInput} type="file" accept="image/jpeg,image/png,image/webp" capture="environment" multiple hidden onChange={(e) => read(e.target.files)} />
        </div>
        <p className="small muted">{t("receipt.hint")}</p>
      </div>

      {receipt && (
        <div className="stack-sm">
          {!receipt.adds_up && (
            <Notice kind="warning">
              {receipt.total != null
                ? t("receipt.notAddingUp", { lines: kr(receipt.lines_sum, locale), total: kr(receipt.total, locale) })
                : t("receipt.noTotal")}
            </Notice>
          )}
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th />
                  <th>{t("receipt.line")}</th>
                  <th>{t("receipt.onList")}</th>
                  <th className="num">{t("receipt.price")}</th>
                </tr>
              </thead>
              <tbody>
                {receipt.lines.map((line, index) => (
                  <tr key={`${line.text}-${index}`}>
                    <td>
                      <input
                        type="checkbox"
                        aria-label={line.name}
                        checked={keep[index] ?? true}
                        onChange={(e) => setKeep((current) => current.map((value, i) => (i === index ? e.target.checked : value)))}
                      />
                    </td>
                    <td>
                      {line.name}
                      <div className="small muted">{line.text}</div>
                    </td>
                    <td>
                      <select
                        aria-label={t("receipt.onList")}
                        value={matches[index] ?? ""}
                        onChange={(e) => setMatches((current) => current.map((value, i) => (i === index ? (e.target.value ? Number(e.target.value) : null) : value)))}
                      >
                        <option value="">{t("receipt.notOnList")}</option>
                        {items.filter((item) => !item.covered_by_pantry).map((item) => (
                          <option key={item.id} value={item.id}>
                            {item.name}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td className="num">{kr(line.price, locale)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {receipt.not_on_receipt.length > 0 && (
            <p className="small muted">{t("receipt.missing", { items: receipt.not_on_receipt.map((item) => item.name).join(", ") })}</p>
          )}
        </div>
      )}

      <NumberField label={t("shopping.receipt")} hint={t("shopping.receiptHint")} value={amount} onChange={setAmount} />
      <div className="row">
        <Button
          variant="primary"
          disabled={busy || reading}
          onClick={() =>
            void run(async () => {
              await api(`/shopping-lists/${listId}/complete`, { body: { amount, receipt: confirmed && confirmed.length > 0 ? confirmed : null } });
              await onDone();
            })
          }
        >
          {receipt ? t("receipt.confirm") : t("shopping.complete")}
        </Button>
      </div>
    </Modal>
  );
}
