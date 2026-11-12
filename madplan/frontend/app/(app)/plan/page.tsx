"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { SavingsTile, StatTile } from "@/components/charts";
import { NearbyList } from "@/components/PlanDish";
import { RecipeDialog, RecipeLinkButton } from "@/components/RecipeDialog";
import { Button, Card, Check, Empty, Field, Icon, Modal, Notice, NumberField, PageHead, Segmented, Stepper } from "@/components/ui";
import { api, ApiError } from "@/lib/api";
import { dayLong, dayShort, kr, todayIso } from "@/lib/format";
import { useAction, useApi, useMe, useToast } from "@/lib/hooks";
import { useI18n, type TranslationKey } from "@/lib/i18n";
import type { Bought, DishSuggestionOption, Meal, MealieEntry, MealType, Plan, PlanSummary, Reason, ShareTarget } from "./types";

/** "Tirsdag 17. november" in the middle of a Danish sentence is written "tirsdag 17. november". */
function inSentence(text: string, locale: string): string {
  return locale === "en" ? text : text.charAt(0).toLowerCase() + text.slice(1);
}

const MEAL_ORDER: MealType[] = ["breakfast", "lunch", "snack", "dinner", "drink"];
type Mode = "full_week" | "workweek" | "days_ahead" | "range";

export default function PlanPage() {
  const { t, locale } = useI18n();
  const me = useMe();
  const { data: plans, reload: reloadPlans } = useApi<PlanSummary[]>("/plans");
  const [selected, setSelected] = useState<number | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [creating, setCreating] = useState(false);
  const [sharing, setSharing] = useState(false);
  const { busy, run } = useAction();
  const { show } = useToast();

  const activeId = selected ?? plans?.[0]?.id ?? null;

  // A link such as the one shown after putting a dish on a day opens that plan.
  useEffect(() => {
    const wanted = Number(new URLSearchParams(window.location.search).get("plan"));
    if (wanted > 0) setSelected(wanted);
  }, []);

  useEffect(() => {
    if (activeId === null) {
      setPlan(null);
      return;
    }
    if (plan?.id === activeId) return;
    void run(async () => setPlan(await api<Plan>(`/plans/${activeId}`)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  const summary = plan?.shopping_list?.summary;
  const { data: mealieEntries, reload: reloadEntries } = useApi<MealieEntry[]>(plan?.is_member && plan.mealie?.connected ? `/plans/${plan.id}/mealie-entries` : null);

  // The worker does the sending, so the page follows along until it is done.
  const sending = plan?.mealie?.sending ?? 0;
  const planId = plan?.id;
  useEffect(() => {
    if (!sending || !planId) return;
    const timer = setInterval(() => {
      void api<Plan>(`/plans/${planId}`).then(setPlan);
    }, 5000);
    return () => {
      clearInterval(timer);
      void reloadEntries();
    };
  }, [sending, planId, reloadEntries]);

  return (
    <>
      <PageHead
        title={t("plan.title")}
        subtitle={t(me.setup.mealie ? "plan.subtitle" : "plan.subtitleNoMealie")}
        actions={
          me.role === "guest" ? undefined : (
            <Button variant="primary" onClick={() => setCreating(true)}>
              {t("plan.new")}
            </Button>
          )
        }
      />

      {plans && plans.length > 1 && (
        <Field label={t("plan.select")}>
          <select value={activeId ?? ""} onChange={(e) => setSelected(Number(e.target.value))}>
            {plans.map((p) => (
              <option key={p.id} value={p.id}>
                {dayShort(p.start_date, locale)} – {dayShort(p.end_date, locale)}
                {p.total != null ? ` · ${kr(p.total, locale)}` : ""}
                {p.shared ? ` · ${t("plan.shared")}` : ""}
              </option>
            ))}
          </select>
        </Field>
      )}

      {plans && plans.length === 0 && !plan && (
        <Card>
          <Empty action={me.role === "guest" ? undefined : <Button variant="primary" onClick={() => setCreating(true)}>{t("plan.generate")}</Button>}>{t("plan.empty")}</Empty>
        </Card>
      )}

      {plan && (
        <>
          {plan.ai_note && <Notice>{t(`plan.aiSkipped.${plan.ai_note}` as TranslationKey)}</Notice>}
          {plan.missing_slots > 0 && plan.is_member && (
            <Notice kind="warning">
              {t("plan.missingSlots", { count: plan.missing_slots })} <Link href="/recipes">{t("plan.findMore")}</Link>
              <div className="row no-print" style={{ marginTop: 6 }}>
                <Button
                  size="sm"
                  disabled={busy}
                  onClick={() =>
                    void run(async () => {
                      const result = await api<{ added: number; plan: Plan }>(`/plans/${plan.id}/fill`, { method: "POST" });
                      setPlan(result.plan);
                      show(result.added > 0 ? t("plan.fill.done", { count: result.added }) : t("plan.fill.none"));
                    })
                  }
                >
                  <Icon name="sparkle" />
                  {t("plan.fill.button")}
                </Button>
              </div>
            </Notice>
          )}
          {summary && (
            <div className="grid-4">
              <StatTile
                label={t("plan.total")}
                value={kr(summary.total, locale)}
                sub={summary.budget ? (summary.within_budget ? t("plan.withinBudget") : t("plan.overBudget")) : undefined}
              />
              <SavingsTile summary={summary} />
              <StatTile
                label={t("nav.shopping")}
                value={<Link href={`/shopping?plan=${plan.id}`}>{plan.shopping_list?.items.filter((i) => !i.covered_by_pantry).length}</Link>}
                sub={t("plan.priceQuality", { real: summary.real_prices, estimated: summary.estimated_prices, unknown: summary.unknown_prices })}
              />
            </div>
          )}

          {summary?.within_budget === false && <Notice kind="warning">{t("plan.overBudget")}: {kr(summary.total, locale)} / {kr(summary.budget, locale)}</Notice>}

          {plan.is_member && (
            <div className="row no-print">
              <Button
                disabled={busy || !plan.meals.some((m) => m.status === "pending")}
                onClick={() => void run(async () => setPlan(await api<Plan>(`/plans/${plan.id}/accept-all`, { method: "POST" })))}
              >
                <Icon name="check" />
                {t("plan.acceptAll")}
              </Button>
              <Button onClick={() => setSharing(true)}>{t("plan.share")}</Button>
              <Button
                variant="ghost"
                className="danger"
                onClick={() => {
                  if (window.confirm(t("plan.deleteConfirm"))) {
                    void run(async () => {
                      await api(`/plans/${plan.id}`, { method: "DELETE" });
                      setPlan(null);
                      setSelected(null);
                      await reloadPlans();
                    });
                  }
                }}
              >
                {t("plan.delete")}
              </Button>
              {plan.ai_used && (
                <span className="badge">
                  <Icon name="sparkle" size={14} />
                  {t("plan.aiUsed")}
                </span>
              )}
            </div>
          )}

          {plan.is_member && plan.mealie && <SendToMealie plan={plan} setPlan={setPlan} />}

          <div className="stack">
            {plan.days.map((day) => (
              <DayCard key={day.date} day={day} plan={plan} setPlan={setPlan} myId={me.id} planned={(mealieEntries ?? []).filter((entry) => entry.date === day.date)} />
            ))}
          </div>
        </>
      )}

      {creating && (
        <CreatePlan
          onClose={() => setCreating(false)}
          onCreated={async (created) => {
            setCreating(false);
            setPlan(created);
            setSelected(created.id);
            await reloadPlans();
          }}
        />
      )}
      {sharing && plan && <SharePlan plan={plan} onClose={() => setSharing(false)} />}
    </>
  );
}

function SendToMealie({ plan, setPlan }: { plan: Plan; setPlan: (plan: Plan) => void }) {
  const { t } = useI18n();
  const { busy, run } = useAction();
  const state = plan.mealie;
  if (!state) return null;
  // Without Mealie set up, there is nothing to send and nothing to say about it.
  if (!state.connected) return null;

  const notes = [];
  if (state.sending > 0) notes.push(t("plan.send.sending", { count: state.sending }));
  else if (state.to_send > 0) notes.push(t("plan.send.waiting", { count: state.to_send }));
  else if (state.sent > 0) notes.push(t("plan.send.allSent"));
  else notes.push(t("plan.send.nothingYet"));
  if (state.undecided > 0) notes.push(t("plan.send.undecided", { count: state.undecided }));

  return (
    <Card>
      <div className="row between">
        <div className="grow small muted" style={{ flexBasis: 240 }}>
          {notes.join(" ")}
          {!state.sync_enabled && ` ${t("plan.send.off")}`}
        </div>
        <Button
          variant="primary"
          disabled={busy || state.to_send === 0 || state.sending > 0 || !state.sync_enabled}
          onClick={() => void run(async () => setPlan(await api<Plan>(`/plans/${plan.id}/send-to-mealie`, { method: "POST" })))}
        >
          <Icon name="check" />
          {t("plan.send.button")}
        </Button>
      </div>
    </Card>
  );
}

function CreatePlan({ onClose, onCreated }: { onClose: () => void; onCreated: (plan: Plan) => void | Promise<void> }) {
  const { t } = useI18n();
  const me = useMe();
  const { data: household } = useApi<{ settings: { default_meal_types: MealType[]; default_adults: number; default_children: number } }>("/household");
  const { busy, run } = useAction();
  const [mode, setMode] = useState<Mode>("full_week");
  const [daysAhead, setDaysAhead] = useState(7);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [mealTypes, setMealTypes] = useState<MealType[] | null>(null);
  const [adults, setAdults] = useState<number | null>(null);
  const [children, setChildren] = useState<number | null>(null);
  const [budget, setBudget] = useState<number | null>(null);
  const [useAi, setUseAi] = useState(true);
  const [noRecipes, setNoRecipes] = useState(false);

  const chosenTypes = mealTypes ?? household?.settings.default_meal_types ?? ["dinner"];
  const currentAdults = adults ?? household?.settings.default_adults ?? me.household.default_adults;
  const currentChildren = children ?? household?.settings.default_children ?? me.household.default_children;

  return (
    <Modal title={t("plan.new")} onClose={onClose}>
      <div className="stack-sm">
        <span className="small strong muted">{t("plan.period")}</span>
        <Segmented
          value={mode}
          label={t("plan.period")}
          onChange={setMode}
          options={(["full_week", "workweek", "days_ahead", "range"] as Mode[]).map((value) => ({ value, label: t(`plan.mode.${value}` as TranslationKey) }))}
        />
      </div>
      {mode === "days_ahead" && (
        <Field label={t("plan.daysAhead")}>
          <input type="number" min={1} max={31} value={daysAhead} onChange={(e) => setDaysAhead(Number(e.target.value))} />
        </Field>
      )}
      {mode === "range" && (
        <div className="form-grid">
          <Field label={t("plan.start")}>
            <input type="date" value={start} onChange={(e) => setStart(e.target.value)} />
          </Field>
          <Field label={t("plan.end")}>
            <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
          </Field>
        </div>
      )}
      <div className="stack-sm">
        <span className="small strong muted">{t("plan.mealTypes")}</span>
        <div className="row" style={{ gap: 14 }}>
          {MEAL_ORDER.map((type) => (
            <Check
              key={type}
              label={t(`meal.${type}` as TranslationKey)}
              checked={chosenTypes.includes(type)}
              onChange={(checked) => setMealTypes(checked ? [...chosenTypes, type] : chosenTypes.filter((m) => m !== type))}
            />
          ))}
        </div>
      </div>
      <div className="row" style={{ gap: 20 }}>
        <div className="stack-sm">
          <span className="small strong muted">{t("common.adults")}</span>
          <Stepper label={t("common.adults")} value={currentAdults} onChange={setAdults} />
        </div>
        <div className="stack-sm">
          <span className="small strong muted">{t("common.children")}</span>
          <Stepper label={t("common.children")} value={currentChildren} onChange={setChildren} />
        </div>
      </div>
      <NumberField label={t("plan.budget")} hint={t("plan.budgetHint")} value={budget} onChange={setBudget} />
      <Check label={t("plan.useAi")} hint={t("plan.useAiHint")} checked={useAi} onChange={setUseAi} />
      {noRecipes && (
        <Notice kind="warning">
          {t("plan.noRecipes")} <Link href="/recipes">{t("plan.findMore")}</Link>
        </Notice>
      )}
      <div className="row">
        <Button
          variant="primary"
          working={busy}
          workingTexts={useAi ? [t("working.plan1"), t("working.plan2"), t("working.plan3")] : [t("working.planNoSearch")]}
          disabled={chosenTypes.length === 0 || (mode === "range" && (!start || !end))}
          onClick={() =>
            void run(async () => {
              setNoRecipes(false);
              try {
                const created = await api<Plan>("/plans", {
                  body: {
                    mode,
                    days_ahead: daysAhead,
                    start: mode === "range" ? start : null,
                    end: mode === "range" ? end : null,
                    meal_types: chosenTypes,
                    adults: currentAdults,
                    children: currentChildren,
                    budget,
                    use_ai: useAi,
                  },
                });
                await onCreated(created);
              } catch (caught) {
                if (caught instanceof ApiError && caught.code === "no_recipes") setNoRecipes(true);
                else throw caught;
              }
            })
          }
        >
          {t("plan.generate")}
        </Button>
        <Button variant="ghost" onClick={onClose}>
          {t("common.cancel")}
        </Button>
      </div>
    </Modal>
  );
}

function DayCard({ day, plan, setPlan, myId, planned }: { day: Plan["days"][number]; plan: Plan; setPlan: (plan: Plan) => void; myId: number; planned: MealieEntry[] }) {
  const { t, locale } = useI18n();
  const { busy, run } = useAction();
  const [guestsOpen, setGuestsOpen] = useState(false);
  const [dropping, setDropping] = useState(false);
  const { show } = useToast();
  const canDrop = plan.is_member && day.date >= todayIso();

  // A meal dragged here moves to this day. A dish of the same meal already here trades places with it.
  const drop = (mealId: number) => {
    const meal = plan.meals.find((m) => m.id === mealId);
    if (!meal || meal.date === day.date) return;
    const there = plan.meals.find((m) => m.date === day.date && m.meal_type === meal.meal_type && m.status !== "rejected");
    void run(async () => {
      setPlan(await api<Plan>(`/meals/${meal.id}/move`, { body: { date: day.date, swap: Boolean(there) } }));
      show(there ? t("plan.move.swapped", { a: meal.title, b: there.title }) : t("plan.move.moved", { title: meal.title, day: dayLong(day.date, locale) }));
    });
  };
  const [suggestions, setSuggestions] = useState<{ meal_id: number; title: string; new_cost: number; alternatives: { recipe_id: number; title: string; cost: number }[] }[]>([]);
  // On a skipped day, what is left is only waiting to come out of Mealie.
  const meals = plan.meals
    .filter((m) => m.date === day.date && !day.skipped)
    .sort((a, b) => MEAL_ORDER.indexOf(a.meal_type) - MEAL_ORDER.indexOf(b.meal_type));

  const patchDay = (body: Record<string, unknown>) =>
    run(async () => {
      const result = await api<{ plan: Plan; suggestions: typeof suggestions }>(`/plans/${plan.id}/days/${day.date}`, { method: "PATCH", body });
      setPlan(result.plan);
      setSuggestions(result.suggestions);
    });

  return (
    <Card className={dropping ? "drop-target" : undefined}>
      <div
        className="day"
        onDragOver={(event) => {
          if (!canDrop || !event.dataTransfer.types.includes("application/x-madplan-meal")) return;
          event.preventDefault();
          event.dataTransfer.dropEffect = "move";
          setDropping(true);
        }}
        onDragLeave={() => setDropping(false)}
        onDrop={(event) => {
          setDropping(false);
          const id = Number(event.dataTransfer.getData("application/x-madplan-meal"));
          if (canDrop && id) {
            event.preventDefault();
            drop(id);
          }
        }}
      >
        <div className="day-head">
          <div className="row">
            <h2>{dayLong(day.date, locale)}</h2>
            {(day.guest_adults > 0 || day.guest_children > 0) && (
              <span className="badge">{t("plan.day.guestCount", { adults: day.guest_adults, children: day.guest_children })}</span>
            )}
          </div>
          {plan.is_member && (
            <div className="row no-print">
              {!day.skipped && (
                <Button size="sm" variant="ghost" onClick={() => setGuestsOpen(true)}>
                  {t("plan.day.guests")}
                </Button>
              )}
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => void patchDay({ skipped: !day.skipped })}>
                {day.skipped ? t("plan.day.unskip") : t("plan.day.skip")}
              </Button>
            </div>
          )}
        </div>
        {day.skipped && <p className="muted">{t("plan.day.skipped")}</p>}
        {planned.length > 0 && (
          <Notice>
            {t("plan.day.inMealie")}{" "}
            {planned.map((entry, index) => {
              const known = ["breakfast", "lunch", "dinner", "snack", "drink"].includes(entry.meal_type);
              return (
                <span key={`${entry.meal_type}-${entry.title}-${index}`}>
                  {index > 0 && ", "}
                  {entry.url ? (
                    <a href={entry.url} target="_blank" rel="noopener noreferrer nofollow">
                      {entry.title}
                    </a>
                  ) : (
                    entry.title
                  )}
                  {known && ` (${t(`meal.${entry.meal_type}` as TranslationKey).toLowerCase()})`}
                  {entry.from_madplan && ` – ${t("calendar.fromOldPlan")}`}
                </span>
              );
            })}
            . {t("plan.day.inMealieNote")}
          </Notice>
        )}
        {suggestions.map((s) => (
          <Notice key={s.meal_id}>
            {t("plan.day.cheaperOptions", { title: s.title, cost: kr(s.new_cost, locale) })}
            <div className="row" style={{ marginTop: 6 }}>
              {s.alternatives.map((a) => (
                <Button
                  size="sm"
                  key={a.recipe_id}
                  onClick={() =>
                    void run(async () => {
                      setPlan(await api<Plan>(`/meals/${s.meal_id}/replace`, { body: { recipe_id: a.recipe_id } }));
                      setSuggestions((current) => current.filter((x) => x.meal_id !== s.meal_id));
                    })
                  }
                >
                  {a.title} · {kr(a.cost, locale)}
                </Button>
              ))}
            </div>
          </Notice>
        ))}
        {meals.map((meal) => (
          <MealCard key={meal.id} meal={meal} plan={plan} setPlan={setPlan} myId={myId} />
        ))}
        {!day.skipped && meals.length === 0 && <p className="muted small">{t("plan.day.noDish")}</p>}
      </div>
      {guestsOpen && (
        <GuestsDialog
          date={dayLong(day.date, locale)}
          adults={day.guest_adults}
          childrenCount={day.guest_children}
          onClose={() => setGuestsOpen(false)}
          onSave={(adults, children) => {
            setGuestsOpen(false);
            void patchDay({ guest_adults: adults, guest_children: children });
          }}
        />
      )}
    </Card>
  );
}

function GuestsDialog({ date, adults, childrenCount, onSave, onClose }: { date: string; adults: number; childrenCount: number; onSave: (adults: number, children: number) => void; onClose: () => void }) {
  const { t } = useI18n();
  const [a, setA] = useState(adults);
  const [c, setC] = useState(childrenCount);
  return (
    <Modal title={t("plan.day.guestsTitle", { date })} onClose={onClose}>
      <p className="muted small">{t("plan.day.guestsHint")}</p>
      <div className="row" style={{ gap: 20 }}>
        <div className="stack-sm">
          <span className="small strong muted">{t("common.adults")}</span>
          <Stepper label={t("common.adults")} value={a} onChange={setA} />
        </div>
        <div className="stack-sm">
          <span className="small strong muted">{t("common.children")}</span>
          <Stepper label={t("common.children")} value={c} onChange={setC} />
        </div>
      </div>
      <div className="row">
        <Button variant="primary" onClick={() => onSave(a, c)}>
          {t("common.save")}
        </Button>
      </div>
    </Modal>
  );
}

function reasonText(reason: Reason, t: ReturnType<typeof useI18n>["t"]) {
  const key = `reason.${reason.code}` as TranslationKey;
  return t(key, { items: reason.items?.join(", "), term: reason.term, days: reason.days });
}

function MealCard({ meal, plan, setPlan, myId }: { meal: Meal; plan: Plan; setPlan: (plan: Plan) => void; myId: number }) {
  const { t, locale } = useI18n();
  const { busy, run } = useAction();
  const [recipeOpen, setRecipeOpen] = useState(false);
  const [altOpen, setAltOpen] = useState(false);
  const [peopleOpen, setPeopleOpen] = useState(false);
  const [moveOpen, setMoveOpen] = useState(false);
  const [leftoversOpen, setLeftoversOpen] = useState(false);
  const isLeftovers = meal.leftover_of !== null;
  const canCookMore = plan.is_member && meal.recipe_id !== null && !isLeftovers && meal.leftovers.length === 0;
  const mine = meal.votes.find((v) => v.user_id === myId)?.vote;
  const allBought = meal.bought === "full";
  const ahead = meal.date >= todayIso();
  const setBought = (bought: Bought) => run(async () => setPlan(await api<Plan>(`/meals/${meal.id}/bought`, { body: { bought } })));
  const sayNo = () => {
    // Some of it is bought already. Saying no is still allowed, but not without a second thought.
    if (meal.bought === "partial" && !window.confirm(t("plan.bought.partialConfirm"))) return;
    void vote("no");
  };
  const title = locale === "en" && meal.title_en ? meal.title_en : meal.title;
  const vote = (value: "yes" | "no") => run(async () => setPlan(await api<Plan>(`/meals/${meal.id}/vote`, { body: { vote: value } })));
  const statusClass = meal.status === "accepted" ? "good" : meal.status === "rejected" ? "critical" : "";
  // Only what has had a chance to be made. It decides what the pantry says is left.
  const canSayMade = plan.is_member && meal.status !== "rejected" && meal.date <= todayIso();
  const setCooked = (cooked: boolean) => run(async () => setPlan(await api<Plan>(`/meals/${meal.id}/cooked`, { body: { cooked } })));

  const draggable = plan.is_member && ahead && meal.status !== "rejected";
  return (
    <div
      className={`meal ${meal.status}${draggable ? " draggable" : ""}`}
      draggable={draggable}
      title={draggable ? t("plan.move.dragHint") : undefined}
      onDragStart={(event) => {
        event.dataTransfer.setData("application/x-madplan-meal", String(meal.id));
        event.dataTransfer.effectAllowed = "move";
      }}
    >
      <div className="row between" style={{ alignItems: "flex-start" }}>
        <div className="grow">
          <div className="small muted">{t(`meal.${meal.meal_type}` as TranslationKey)}</div>
          {meal.recipe_id ? (
            <button type="button" className="meal-title" onClick={() => setRecipeOpen(true)}>
              {title}
            </button>
          ) : (
            <span className="strong">{title}</span>
          )}
          {meal.added_by && <div className="small muted">{t("plan.meal.chosenBy", { name: meal.added_by.name })}</div>}
          {meal.leftover_of && (
            <div className="small muted">{t("plan.leftovers.from", { title: meal.leftover_of.title, day: inSentence(dayLong(meal.leftover_of.date, locale), locale) })}</div>
          )}
          {meal.leftovers.map((left) => (
            <div className="small muted" key={left.id}>
              {t("plan.leftovers.cookedFor", { portions: meal.leftover_portions, day: inSentence(dayLong(left.date, locale), locale), meal: t(`meal.${left.meal_type}` as TranslationKey).toLowerCase() })}
            </div>
          ))}
        </div>
        <div className="right">
          <div className="num strong">{kr(meal.estimated_cost, locale)}</div>
          {meal.saving > 0 && <div className="small delta-good">{t("plan.meal.saving", { amount: kr(meal.saving, locale) })}</div>}
        </div>
      </div>

      {meal.reasons.length > 0 && (
        <div className="reason">{meal.reasons.slice(0, 3).map((r) => reasonText(r, t)).join(" · ")}</div>
      )}

      {meal.status !== "rejected" && meal.also_planned.length > 0 && (
        <p className="small delta-bad">
          <Icon name="alert" size={13} /> {t("plan.meal.alsoPlanned")} <NearbyList meals={meal.also_planned} />
        </p>
      )}

      {meal.recipe_id && (
        <div className="row small">
          {meal.link ? <RecipeLinkButton link={meal.link} /> : <span className="muted">{t("plan.meal.noSource")}</span>}
        </div>
      )}

      <div className="row between">
        <div className="row small">
          <span className={`badge ${statusClass}`}>
            {meal.status === "accepted" && <Icon name="check" size={13} />}
            {t(`plan.status.${meal.status}` as TranslationKey)}
          </span>
          {meal.sync_status !== "none" && (
            <span className={`badge ${meal.sync_status === "synced" ? "good" : meal.sync_status === "failed" ? "critical" : ""}`} title={meal.sync_error ?? undefined}>
              {t(`plan.sync.${meal.sync_status}` as TranslationKey)}
            </span>
          )}
          {meal.sync_status === "failed" && plan.is_member && (
            <Button
              size="sm"
              variant="ghost"
              onClick={() =>
                void run(async () => {
                  await api(`/meals/${meal.id}/retry-sync`, { method: "POST" });
                  setPlan(await api<Plan>(`/plans/${plan.id}`));
                })
              }
            >
              {t("plan.sync.retry")}
            </Button>
          )}
          {meal.cooked !== null && (
            <span className={`badge ${meal.cooked ? "good" : ""}`}>{meal.cooked ? t("plan.cooked.yes") : t("plan.cooked.no")}</span>
          )}
          {meal.bought !== "none" && meal.status !== "rejected" && (
            <span className={`badge ${allBought ? "good" : "warning"}`}>
              <Icon name="cart" size={13} />
              {t(`plan.bought.${meal.bought}` as TranslationKey)}
            </span>
          )}
          {meal.voters > 1 && <span className="muted">{t("plan.meal.votes", { count: meal.votes.length, total: meal.voters })}</span>}
          {plan.is_member ? (
            <button type="button" className="btn ghost sm" onClick={() => setPeopleOpen(true)}>
              {t("common.people", { adults: meal.adults, children: meal.children })}
            </button>
          ) : (
            <span className="muted">{t("common.people", { adults: meal.adults, children: meal.children })}</span>
          )}
        </div>
        {canSayMade && (
          <div className="row no-print">
            {meal.cooked !== true && (
              <Button size="sm" disabled={busy} onClick={() => void setCooked(true)} title={t("plan.cooked.hint")}>
                {t("plan.cooked.made")}
              </Button>
            )}
            {meal.cooked !== false && (
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => void setCooked(false)} title={t("plan.cooked.hint")}>
                {t("plan.cooked.notMade")}
              </Button>
            )}
          </div>
        )}
        {plan.can_vote && meal.date >= todayIso() && !isLeftovers && (
          <div className="row no-print">
            <Button size="sm" className="yes" aria-pressed={mine === "yes"} disabled={busy} onClick={() => void vote("yes")}>
              {t("common.yes")}
            </Button>
            <Button size="sm" className="no" aria-pressed={mine === "no"} disabled={busy || allBought} title={allBought ? t("plan.bought.noBlocked") : undefined} onClick={sayNo}>
              {t("common.no")}
            </Button>
            <Button size="sm" variant="ghost" disabled={allBought} title={allBought ? t("plan.bought.noBlocked") : undefined} onClick={() => setAltOpen(true)}>
              {t("plan.meal.other")}
            </Button>
          </div>
        )}
      </div>

      {plan.is_member && ahead && meal.status !== "rejected" && (
        <div className="row small no-print">
          <label className="inline-select">
            <span className="muted">{t("plan.bought.label")}</span>
            <select value={meal.bought} disabled={busy} onChange={(e) => void setBought(e.target.value as Bought)}>
              {(["none", "partial", "full"] as Bought[]).map((value) => (
                <option key={value} value={value}>
                  {t(`plan.bought.option.${value}` as TranslationKey)}
                </option>
              ))}
            </select>
          </label>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => setMoveOpen(true)}>
            <Icon name="calendar" size={14} />
            {t("plan.move.button")}
          </Button>
          {canCookMore && (
            <Button size="sm" variant="ghost" disabled={busy} onClick={() => setLeftoversOpen(true)}>
              {t("plan.leftovers.button")}
            </Button>
          )}
          {(isLeftovers || meal.leftovers.length > 0) && (
            <Button
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={() => void run(async () => setPlan(await api<Plan>(`/meals/${meal.id}/leftovers`, { method: "DELETE" })))}
            >
              {t("plan.leftovers.stop")}
            </Button>
          )}
        </div>
      )}
      {allBought && ahead && meal.status !== "rejected" && <p className="small muted">{t("plan.bought.noBlocked")}</p>}

      {meal.sync_status === "failed" && meal.sync_error?.startsWith("import_") && (
        <p className="small delta-bad">{t(`error.${meal.sync_error}` as TranslationKey)}</p>
      )}

      {meal.votes.filter((v) => v.comment).map((v) => (
        <p className="small muted" key={v.user_id}>
          {v.name}: {v.comment}
        </p>
      ))}
      {meal.alternatives.length > 0 && (
        <p className="small muted">
          {t("plan.alt.proposed")}: {meal.alternatives.map((a) => a.title).join(", ")}
        </p>
      )}

      {recipeOpen && meal.recipe_id && <RecipeDialog id={meal.recipe_id} onClose={() => setRecipeOpen(false)} />}
      {altOpen && <AlternativesDialog meal={meal} plan={plan} setPlan={setPlan} onClose={() => setAltOpen(false)} />}
      {peopleOpen && <PeopleDialog meal={meal} setPlan={setPlan} onClose={() => setPeopleOpen(false)} />}
      {moveOpen && <MoveDialog meal={meal} plan={plan} setPlan={setPlan} onClose={() => setMoveOpen(false)} />}
      {leftoversOpen && <LeftoversDialog meal={meal} plan={plan} setPlan={setPlan} onClose={() => setLeftoversOpen(false)} />}
    </div>
  );
}

const MEALS_IN_A_DAY: MealType[] = ["breakfast", "lunch", "snack", "dinner", "drink"];

function LeftoversDialog({ meal, plan, setPlan, onClose }: { meal: Meal; plan: Plan; setPlan: (plan: Plan) => void; onClose: () => void }) {
  const { t, locale } = useI18n();
  const { busy, run } = useAction();
  const days = plan.days.filter((d) => d.date >= meal.date && d.date >= todayIso());
  const next = days.find((d) => d.date > meal.date)?.date ?? meal.date;
  const [day, setDay] = useState(next);
  const [mealType, setMealType] = useState<MealType>("lunch");
  const [people, setPeople] = useState(plan.meals.find((m) => m.id === meal.id)?.adults ?? 2);
  const taken = plan.meals.find((m) => m.date === day && m.meal_type === mealType && m.status !== "rejected" && m.id !== meal.id);

  return (
    <Modal title={t("plan.leftovers.title", { title: meal.title })} onClose={onClose}>
      <p className="small muted">{t("plan.leftovers.hint")}</p>
      <div className="form-grid">
        <Field label={t("plan.move.day")}>
          <select value={day} onChange={(e) => setDay(e.target.value)}>
            {days.map((d) => (
              <option key={d.date} value={d.date}>
                {dayLong(d.date, locale)}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t("pick.mealType")}>
          <select value={mealType} onChange={(e) => setMealType(e.target.value as MealType)}>
            {MEALS_IN_A_DAY.map((type) => (
              <option key={type} value={type}>
                {t(`meal.${type}` as TranslationKey)}
              </option>
            ))}
          </select>
        </Field>
      </div>
      <div className="stack-sm">
        <span className="small strong muted">{t("plan.leftovers.portions")}</span>
        <Stepper label={t("plan.leftovers.portions")} value={people} min={1} onChange={setPeople} />
      </div>
      {taken && <Notice kind="warning">{t("plan.leftovers.taken", { title: taken.title })}</Notice>}
      <div className="row">
        <Button
          variant="primary"
          disabled={busy || Boolean(taken)}
          onClick={() =>
            void run(async () => {
              setPlan(await api<Plan>(`/meals/${meal.id}/leftovers`, { body: { date: day, meal_type: mealType, portions: people } }));
              onClose();
            })
          }
        >
          {t("plan.leftovers.save")}
        </Button>
        <Button variant="ghost" onClick={onClose}>
          {t("common.cancel")}
        </Button>
      </div>
    </Modal>
  );
}

function MoveDialog({ meal, plan, setPlan, onClose }: { meal: Meal; plan: Plan; setPlan: (plan: Plan) => void; onClose: () => void }) {
  const { t, locale } = useI18n();
  const { busy, run } = useAction();
  const today = todayIso();
  const days = plan.days.filter((d) => d.date >= today && d.date !== meal.date);
  const occupant = (date: string) => plan.meals.find((m) => m.date === date && m.meal_type === meal.meal_type && m.status !== "rejected" && m.id !== meal.id);
  const [target, setTarget] = useState(days.find((d) => !occupant(d.date))?.date ?? days[0]?.date ?? "");
  const there = target ? occupant(target) : undefined;

  return (
    <Modal title={t("plan.move.title", { title: meal.title })} onClose={onClose}>
      <p className="small muted">{t("plan.move.hint")}</p>
      {days.length === 0 ? (
        <p className="muted">{t("plan.move.noDays")}</p>
      ) : (
        <Field label={t("plan.move.day")}>
          <select value={target} onChange={(e) => setTarget(e.target.value)}>
            {days.map((d) => {
              const taken = occupant(d.date);
              return (
                <option key={d.date} value={d.date}>
                  {dayLong(d.date, locale)}
                  {taken ? ` – ${t("plan.move.swapWith", { title: taken.title })}` : ""}
                </option>
              );
            })}
          </select>
        </Field>
      )}
      <div className="row">
        <Button
          variant="primary"
          disabled={busy || !target}
          onClick={() =>
            void run(async () => {
              setPlan(await api<Plan>(`/meals/${meal.id}/move`, { body: { date: target, swap: Boolean(there) } }));
              onClose();
            })
          }
        >
          {there ? t("plan.move.swap", { title: there.title }) : t("plan.move.save")}
        </Button>
        <Button variant="ghost" onClick={onClose}>
          {t("common.cancel")}
        </Button>
      </div>
    </Modal>
  );
}

function PeopleDialog({ meal, setPlan, onClose }: { meal: Meal; setPlan: (plan: Plan) => void; onClose: () => void }) {
  const { t } = useI18n();
  const { busy, run } = useAction();
  const [adults, setAdults] = useState(meal.adults);
  const [children, setChildren] = useState(meal.children);
  return (
    <Modal title={t("plan.meal.peopleTitle")} onClose={onClose}>
      <div className="row" style={{ gap: 20 }}>
        <div className="stack-sm">
          <span className="small strong muted">{t("common.adults")}</span>
          <Stepper label={t("common.adults")} value={adults} onChange={setAdults} />
        </div>
        <div className="stack-sm">
          <span className="small strong muted">{t("common.children")}</span>
          <Stepper label={t("common.children")} value={children} onChange={setChildren} />
        </div>
      </div>
      <div className="row">
        <Button
          variant="primary"
          disabled={busy}
          onClick={() =>
            void run(async () => {
              setPlan(await api<Plan>(`/meals/${meal.id}/people`, { method: "PATCH", body: { adults, children } }));
              onClose();
            })
          }
        >
          {t("common.save")}
        </Button>
      </div>
    </Modal>
  );
}

function AlternativesDialog({ meal, plan, setPlan, onClose }: { meal: Meal; plan: Plan; setPlan: (plan: Plan) => void; onClose: () => void }) {
  const { t, locale } = useI18n();
  const { busy, run } = useAction();
  const { busy: searching, run: runSearch } = useAction();
  const { show } = useToast();
  const { data: options } = useApi<DishSuggestionOption[]>(plan.is_member ? `/meals/${meal.id}/options` : null);
  const [text, setText] = useState("");

  const replace = (recipeId: number) =>
    run(async () => {
      setPlan(await api<Plan>(`/meals/${meal.id}/replace`, { body: { recipe_id: recipeId } }));
      onClose();
    });

  return (
    <Modal title={t("plan.alt.title")} onClose={onClose}>
      {options && options.length > 0 && (
        <div>
          <h3>{t("plan.alt.options")}</h3>
          {options.map((option) => (
            <div className="list-row" key={option.recipe_id}>
              <span className="grow">{locale === "en" && option.title_en ? option.title_en : option.title}</span>
              <RecipeLinkButton link={option.link} />
              <span className="num small">{kr(option.cost, locale)}</span>
              <Button size="sm" disabled={busy || searching} onClick={() => void replace(option.recipe_id)}>
                {t("plan.alt.use")}
              </Button>
            </div>
          ))}
        </div>
      )}
      {meal.alternatives.filter((a) => a.recipe_id).length > 0 && plan.is_member && (
        <div>
          <h3>{t("plan.alt.proposed")}</h3>
          {meal.alternatives.filter((a) => a.recipe_id).map((alternative) => (
            <div className="list-row" key={alternative.id}>
              <span className="grow">{alternative.title}</span>
              <RecipeLinkButton link={alternative.link} />
              <span className="badge">{t(`plan.alt.source.${alternative.source}` as TranslationKey)}</span>
              <Button size="sm" disabled={busy || searching} onClick={() => void replace(alternative.recipe_id as number)}>
                {t("plan.alt.use")}
              </Button>
            </div>
          ))}
        </div>
      )}
      {plan.is_member && (
        <Button
          working={searching}
          workingTexts={[t("working.another1"), t("working.reading"), t("working.minute")]}
          disabled={busy}
          onClick={() =>
            void runSearch(async () => {
              const updated = await api<Plan>(`/meals/${meal.id}/ai-alternative`, { method: "POST" });
              setPlan(updated);
              const added = updated.meals.find((m) => m.id === meal.id)?.alternatives.at(-1);
              if (added) show(`${t("plan.alt.proposed")}: ${added.title}`);
              onClose();
            })
          }
        >
          <Icon name="sparkle" />
          {t("plan.alt.ask")}
        </Button>
      )}
      <Field label={t("plan.alt.yours")} hint={t("plan.alt.yoursHint")}>
        <input value={text} maxLength={200} onChange={(e) => setText(e.target.value)} />
      </Field>
      <div className="row">
        <Button
          variant="primary"
          disabled={busy || searching || !text.trim()}
          onClick={() =>
            void run(async () => {
              setPlan(await api<Plan>(`/meals/${meal.id}/alternatives`, { body: { title: text.trim() } }));
              onClose();
            })
          }
        >
          {t("plan.alt.send")}
        </Button>
      </div>
    </Modal>
  );
}

function SharePlan({ plan, onClose }: { plan: Plan; onClose: () => void }) {
  const { t } = useI18n();
  const { data: targets, reload } = useApi<ShareTarget[]>(`/plans/${plan.id}/share-targets`);
  const { run } = useAction();
  return (
    <Modal title={t("plan.share.title")} onClose={onClose}>
      <p className="small muted">{t("plan.share.hint")}</p>
      {targets && targets.length === 0 && <p className="muted">{t("plan.share.none")}</p>}
      {targets?.map((target) => (
        <div className="list-row" key={target.id}>
          <span className="grow">
            {target.display_name}
            {target.same_household && <span className="small muted"> · {t("plan.share.sameHousehold")}</span>}
          </span>
          {!target.same_household && (
            <Button
              size="sm"
              disabled={target.shared}
              onClick={() =>
                void run(async () => {
                  await api(`/plans/${plan.id}/share`, { body: { user_ids: [target.id] } });
                  await reload();
                })
              }
            >
              {target.shared ? t("common.saved") : t("plan.share")}
            </Button>
          )}
        </div>
      ))}
    </Modal>
  );
}
