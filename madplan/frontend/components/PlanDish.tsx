"use client";

import Link from "next/link";
import { useState } from "react";
import { Button, Field, Icon, Modal, Notice } from "@/components/ui";
import { api } from "@/lib/api";
import { dayLong, dayShort, todayIso } from "@/lib/format";
import { useAction, useApi } from "@/lib/hooks";
import { useI18n, type TranslationKey } from "@/lib/i18n";
import type { DishPlanning, MealType, NearbyMeal } from "@/lib/types";

const MEAL_ORDER: MealType[] = ["breakfast", "lunch", "snack", "dinner", "drink"];
// As far ahead as the backend allows a dish to be put on a day.
const MAX_DAYS_AHEAD = 62;

function addDays(iso: string, days: number): string {
  const day = new Date(`${iso}T12:00:00`);
  day.setDate(day.getDate() + days);
  return `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`;
}

/** "tir. 17. nov. (aftensmad)", for lists of where a dish is planned already. */
export function NearbyList({ meals }: { meals: NearbyMeal[] }) {
  const { t, locale } = useI18n();
  return (
    <>
      {meals.map((meal, index) => (
        <span key={meal.meal_id}>
          {index > 0 && ", "}
          {dayShort(meal.date, locale)} ({t(`meal.${meal.meal_type}` as TranslationKey).toLowerCase()}
          {meal.status === "pending" ? `, ${t("plan.status.pending").toLowerCase()}` : ""})
        </span>
      ))}
    </>
  );
}

/** A button that lets someone put this dish on a day of their choosing. */
export function PlanDishButton({ recipeId, title, mealTypes, variant }: { recipeId: number; title: string; mealTypes?: string[] | null; variant?: "primary" | "ghost" }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button size="sm" variant={variant} onClick={() => setOpen(true)}>
        <Icon name="calendar" size={14} />
        {t("pick.button")}
      </Button>
      {open && <PlanDishDialog recipeId={recipeId} title={title} mealTypes={mealTypes} onClose={() => setOpen(false)} />}
    </>
  );
}

export function PlanDishDialog({ recipeId, title, mealTypes, onClose }: { recipeId: number; title: string; mealTypes?: string[] | null; onClose: () => void }) {
  const { t, locale } = useI18n();
  const { busy, run } = useAction();
  const today = todayIso();
  const first = MEAL_ORDER.find((m) => mealTypes?.includes(m)) ?? "dinner";
  const [day, setDay] = useState(addDays(today, 1));
  const [mealType, setMealType] = useState<MealType>(mealTypes?.includes("dinner") ? "dinner" : first);
  const [done, setDone] = useState<{ planId: number; day: string } | null>(null);
  const valid = day >= today && day <= addDays(today, MAX_DAYS_AHEAD);
  const { data: planning } = useApi<DishPlanning>(valid ? `/recipes/${recipeId}/planning?day=${day}&meal_type=${mealType}` : null);
  const slot = planning?.slot ?? null;
  const blocked = Boolean(planning?.allergy) || slot?.bought === "full";

  if (done) {
    return (
      <Modal title={title} onClose={onClose}>
        <Notice kind="good">{t("pick.done", { title, day: dayLong(done.day, locale) })}</Notice>
        <p className="small muted">{t("pick.doneHint")}</p>
        <div className="row">
          <Link className="btn primary" href={`/plan?plan=${done.planId}`}>
            {t("pick.openPlan")}
          </Link>
          <Button variant="ghost" onClick={onClose}>
            {t("common.close")}
          </Button>
        </div>
      </Modal>
    );
  }

  return (
    <Modal title={t("pick.title", { title })} onClose={onClose}>
      <div className="form-grid">
        <Field label={t("pick.day")}>
          <input type="date" value={day} min={today} max={addDays(today, MAX_DAYS_AHEAD)} required onChange={(e) => setDay(e.target.value)} />
        </Field>
        <Field label={t("pick.mealType")}>
          <select value={mealType} onChange={(e) => setMealType(e.target.value as MealType)}>
            {MEAL_ORDER.map((type) => (
              <option key={type} value={type}>
                {t(`meal.${type}` as TranslationKey)}
              </option>
            ))}
          </select>
        </Field>
      </div>

      {planning?.allergy && <Notice kind="critical">{t("error.allergy", { item: planning.allergy })}</Notice>}
      {planning && planning.too_close.length > 0 && (
        <Notice kind="warning">
          {t("pick.tooClose", { days: planning.window_days })} <NearbyList meals={planning.too_close} />.
        </Notice>
      )}
      {planning && planning.too_close.length === 0 && planning.planned.length > 0 && (
        <Notice>
          {t("pick.alsoPlanned")} <NearbyList meals={planning.planned} />.
        </Notice>
      )}
      {slot && slot.bought === "full" && <Notice kind="critical">{t("pick.slotBought", { title: slot.title })}</Notice>}
      {slot && slot.bought !== "full" && (
        <Notice kind="warning">
          {t("pick.slotTaken", { title: slot.title })}
          {slot.bought === "partial" && ` ${t("pick.slotPartly")}`}
        </Notice>
      )}
      <p className="small muted">{t("pick.hint")}</p>

      <div className="row">
        <Button
          variant="primary"
          disabled={busy || !valid || !planning || blocked}
          onClick={() =>
            void run(async () => {
              const result = await api<{ plan_id: number }>("/plans/add-meal", { body: { recipe_id: recipeId, date: day, meal_type: mealType, replace: Boolean(slot) } });
              setDone({ planId: result.plan_id, day });
            })
          }
        >
          {slot ? t("pick.replace", { title: slot.title }) : t("pick.add")}
        </Button>
        <Button variant="ghost" onClick={onClose}>
          {t("common.cancel")}
        </Button>
      </div>
    </Modal>
  );
}
