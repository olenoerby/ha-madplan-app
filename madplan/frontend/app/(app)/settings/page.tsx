"use client";

import { Fragment, useEffect, useState } from "react";
import { Button, Card, Check, Field, FrequencyInput, Icon, Loading, Notice, NumberField, PageHead, SaveBar, Segmented, TagInput } from "@/components/ui";
import { api, BASE } from "@/lib/api";
import { dateTime, number } from "@/lib/format";
import { useAction, useApi, useMe, useSession, useToast } from "@/lib/hooks";
import { useI18n, type TranslationKey } from "@/lib/i18n";
import { type DietMode, type FeatureToggles, type HouseholdSettings, type MealType, type StoreRow, type UserSettings } from "@/lib/types";

type Tab = "profile" | "food" | "features" | "stores" | "household" | "tokens";
const TABS: Tab[] = ["profile", "food", "features", "stores", "household", "tokens"];
const DIETS: DietMode[] = ["mediterranean", "vegetarian", "vegan", "keto", "low_carb", "pescetarian"];
const MEALS: MealType[] = ["breakfast", "lunch", "dinner", "snack", "drink"];

export default function SettingsPage() {
  const { t } = useI18n();
  const [tab, setTab] = useState<Tab>("profile");

  return (
    <>
      <PageHead title={t("settings.title")} />
      <div>
        <Segmented value={tab} onChange={setTab} options={TABS.map((value) => ({ value, label: t(`settings.tab.${value}` as TranslationKey) }))} />
      </div>
      {tab === "profile" && (
        <>
          <ProfileTab />
          <MyDataCard />
        </>
      )}
      {tab === "food" && <FoodTab />}
      {tab === "features" && <FeaturesTab />}
      {tab === "stores" && <StoresTab />}
      {tab === "household" && <HouseholdTab />}
      {tab === "tokens" && <TokensTab />}
    </>
  );
}

function ProfileTab() {
  const { t } = useI18n();
  const me = useMe();
  return (
    <Card title={t("settings.tab.profile")}>
      <p>
        <span className="strong">{me.display_name}</span> <span className="muted small">({me.username})</span>
      </p>
      <p className="small muted">{t("settings.profile.fromHomeAssistant")}</p>
    </Card>
  );
}

function useMySettings() {
  const { refresh } = useSession();
  const { data, setData } = useApi<UserSettings>("/me/settings");
  const { run, busy } = useAction();
  const [saved, setSaved] = useState(false);
  const save = () =>
    run(async () => {
      await api("/me/settings", { method: "PUT", body: data });
      setSaved(true);
      setTimeout(() => setSaved(false), 2500);
      await refresh();
    });
  const change = (patch: Partial<UserSettings>) => {
    setSaved(false);
    setData((current) => (current ? { ...current, ...patch } : current));
  };
  return { data, change, save, busy, saved };
}

function MyDataCard() {
  const { t } = useI18n();
  const me = useMe();
  const { busy, run } = useAction();
  const [confirming, setConfirming] = useState("");
  const [open, setOpen] = useState(false);
  return (
    <Card title={t("settings.data.title")}>
      <p className="small muted">{t("settings.data.hint")}</p>
      <div className="row">
        <a className="btn" href={`${BASE}/api/v1/me/export`} download>
          {t("settings.data.export")}
        </a>
        <Button variant="ghost" className="danger" onClick={() => setOpen(!open)}>
          {t("settings.data.delete")}
        </Button>
      </div>
      {open && (
        <div className="stack-sm">
          <Notice kind="critical">{t("settings.data.deleteWarning")}</Notice>
          <Field label={t("settings.data.deleteConfirm", { username: me.username })}>
            <input value={confirming} autoComplete="off" onChange={(e) => setConfirming(e.target.value)} />
          </Field>
          <div>
            <Button
              className="danger"
              disabled={busy || confirming.trim().toLowerCase() !== me.username}
              onClick={() =>
                void run(async () => {
                  await api("/me/delete", { body: { confirm_username: confirming } });
                  window.location.reload();
                })
              }
            >
              {t("settings.data.deleteNow")}
            </Button>
          </div>
        </div>
      )}
    </Card>
  );
}

function FoodTab() {
  const { t } = useI18n();
  const { data, change, save, busy, saved } = useMySettings();
  const { data: taste } = useApi<{
    liked: { term: string; yes: number; no: number }[];
    rejected: { term: string; yes: number; no: number }[];
    members: { name: string; liked: { term: string }[]; rejected: { term: string }[] }[];
  }>("/household/taste");
  if (!data) return <Loading />;
  return (
    <>
      <Card title={t("settings.tab.food")}>
        <TagInput label={t("settings.allergies")} hint={t("settings.allergiesHint")} values={data.allergies} onChange={(allergies) => change({ allergies })} />
        <TagInput label={t("settings.dislikes")} hint={t("settings.dislikesHint")} values={data.dislikes} onChange={(dislikes) => change({ dislikes })} />
        <TagInput label={t("settings.likes")} hint={t("settings.likesHint")} values={data.likes} onChange={(likes) => change({ likes })} />
        <FrequencyInput values={data.frequencies ?? {}} onChange={(frequencies) => change({ frequencies })} />
        <TagInput label={t("settings.meat")} hint={t("settings.meatHint")} values={data.meat_preferences} onChange={(meat_preferences) => change({ meat_preferences })} />
        <div className="form-grid">
          <Field label={t("settings.diet")}>
            <select value={data.diet_mode ?? ""} onChange={(e) => change({ diet_mode: (e.target.value || null) as DietMode | null })}>
              <option value="">{t("diet.mode.none")}</option>
              {DIETS.map((diet) => (
                <option key={diet} value={diet}>
                  {t(`diet.mode.${diet}` as TranslationKey)}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("settings.dietCoach")}>
            <select
              value={data.diet_coach_enabled === null ? "" : String(data.diet_coach_enabled)}
              onChange={(e) => change({ diet_coach_enabled: e.target.value === "" ? null : e.target.value === "true" })}
            >
              <option value="">{t("settings.dietCoach.inherit")}</option>
              <option value="true">{t("common.on")}</option>
              <option value="false">{t("common.off")}</option>
            </select>
          </Field>
          <NumberField label={t("settings.maxPerMeal")} value={data.max_price_per_meal} onChange={(max_price_per_meal) => change({ max_price_per_meal })} />
          <NumberField label={t("settings.personalBudget")} hint={t("settings.personalBudgetHint")} value={data.budget_weekly} onChange={(budget_weekly) => change({ budget_weekly })} />
        </div>
        <SaveBar busy={busy} saved={saved} onSave={() => void save()} />
      </Card>
      <Card title={t("settings.learned")}>
        {taste && taste.liked.length === 0 && taste.rejected.length === 0 && <p className="muted">{t("settings.learned.none")}</p>}
        {taste && taste.liked.length > 0 && (
          <div className="row">
            <span className="small muted">{t("settings.learned.yes")}:</span>
            {taste.liked.map((entry) => (
              <span className="badge good" key={entry.term}>
                {entry.term}
              </span>
            ))}
          </div>
        )}
        {taste && taste.rejected.length > 0 && (
          <div className="row">
            <span className="small muted">{t("settings.learned.no")}:</span>
            {taste.rejected.map((entry) => (
              <span className="badge" key={entry.term}>
                {entry.term}
              </span>
            ))}
          </div>
        )}
        {taste && taste.members.length > 1 && (
          <div>
            <h3>{t("settings.learned.perPerson")}</h3>
            {taste.members.map((member) => (
              <div className="list-row small" key={member.name}>
                <span className="strong">{member.name}</span>
                <span className="grow muted">
                  {member.liked.length > 0 && `${t("settings.learned.yes")}: ${member.liked.map((e) => e.term).join(", ")}. `}
                  {member.rejected.length > 0 && `${t("settings.learned.no")}: ${member.rejected.map((e) => e.term).join(", ")}.`}
                  {member.liked.length === 0 && member.rejected.length === 0 && t("settings.learned.tooFew")}
                </span>
              </div>
            ))}
          </div>
        )}
      </Card>
    </>
  );
}

function FeaturesTab() {
  const { t } = useI18n();
  const { data, change, save, busy, saved } = useMySettings();
  const { data: notify } = useApi<{ available: { service: string; name: string }[]; default: string | null }>("/me/notify-services");
  if (!data) return <Loading />;
  const features = Object.keys(data.features) as (keyof FeatureToggles)[];
  return (
    <Card title={t("settings.tab.features")}>
      <div className="stack">
        {features.map((feature) => (
          <Check
            key={feature}
            label={t(`settings.feature.${feature}` as TranslationKey)}
            checked={data.features[feature]}
            onChange={(value) => change({ features: { ...data.features, [feature]: value } })}
          />
        ))}
        {data.features.ha_notifications && (
          <Field label={t("settings.notify.label")} hint={t("settings.notify.hint")}>
            <select value={data.notify_service ?? ""} onChange={(e) => change({ notify_service: e.target.value || null })}>
              <option value="">{notify?.default ? t("settings.notify.default", { service: notify.default }) : t("settings.notify.none")}</option>
              {(notify?.available ?? []).map((option) => (
                <option key={option.service} value={option.service}>
                  {option.name === option.service.slice(7) ? option.service : `${option.name} (${option.service})`}
                </option>
              ))}
              {data.notify_service && !notify?.available.some((o) => o.service === data.notify_service) && <option value={data.notify_service}>{data.notify_service}</option>}
            </select>
          </Field>
        )}
        <div className="divider" />
        <Check label={t("settings.photoAutoApply")} checked={data.pantry_photo_auto_apply} onChange={(pantry_photo_auto_apply) => change({ pantry_photo_auto_apply })} />
        <Check label={t("settings.phoneLocation")} hint={t("settings.phoneLocationHint")} checked={data.use_phone_location} onChange={(use_phone_location) => change({ use_phone_location })} />
      </div>
      <SaveBar busy={busy} saved={saved} onSave={() => void save()} />
    </Card>
  );
}

function StoresTab() {
  const { t, locale } = useI18n();
  const me = useMe();
  const [position, setPosition] = useState<{ lat: number; lon: number } | null>(null);
  const path = position ? `/stores?lat=${position.lat}&lon=${position.lon}` : "/stores";
  const { data: stores, reload } = useApi<StoreRow[]>(path);
  const { run } = useAction();
  // The household's store choice belongs to whoever runs the household.
  const isAdmin = me.role === "admin" || me.role === "household_manager";
  const [scope, setScope] = useState<"mine" | "household">("mine");

  const update = (store: StoreRow, patch: Partial<StoreRow>) =>
    run(async () => {
      if (scope === "household") {
        await api(`/household/stores/${store.id}`, { method: "PUT", body: patch });
      } else {
        await api(`/me/stores/${store.id}`, { method: "PUT", body: { enabled: patch.enabled ?? store.enabled, favorite: patch.favorite ?? store.favorite } });
      }
      await reload();
    });

  return (
    <Card title={t("settings.tab.stores")}>
      <p className="small muted">{t("settings.storesHint")}</p>
      <div className="row between">
        {isAdmin && (
          <Segmented
            value={scope}
            onChange={setScope}
            options={[
              { value: "mine", label: t("settings.store.mine") },
              { value: "household", label: t("settings.store.household") },
            ]}
          />
        )}
        {me.settings.use_phone_location && (
          <Button
            size="sm"
            onClick={() =>
              navigator.geolocation.getCurrentPosition((p) => setPosition({ lat: p.coords.latitude, lon: p.coords.longitude }))
            }
          >
            {t("settings.useMyPosition")}
          </Button>
        )}
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th />
              <th>{t("settings.store.enabled")}</th>
              <th>{t("settings.store.favorite")}</th>
              <th>{t("settings.store.distance")}</th>
            </tr>
          </thead>
          <tbody>
            {stores?.map((store) => (
              <tr key={store.id}>
                <td>
                  <span className="strong">{store.name}</span>
                  {store.branch_name && store.branch_name !== store.name && <div className="small muted">{store.branch_name}</div>}
                </td>
                <td>
                  <input type="checkbox" aria-label={`${t("settings.store.enabled")}: ${store.name}`} checked={store.enabled} onChange={(e) => void update(store, { enabled: e.target.checked })} />
                </td>
                <td>
                  <input type="checkbox" aria-label={`${t("settings.store.favorite")}: ${store.name}`} checked={store.favorite} onChange={(e) => void update(store, { favorite: e.target.checked })} />
                </td>
                <td style={{ minWidth: 110 }}>
                  {scope === "household" ? (
                    <input
                      type="number"
                      min={0}
                      step="0.1"
                      aria-label={`${t("settings.store.distance")}: ${store.name}`}
                      defaultValue={store.distance_km ?? ""}
                      onBlur={(e) => {
                        if (e.target.value !== "" && Number(e.target.value) !== store.distance_km) void update(store, { distance_km: Number(e.target.value) });
                      }}
                    />
                  ) : store.distance_km != null ? (
                    <span className="num">{t("settings.store.km", { km: number(store.distance_km, locale, 1) })}</span>
                  ) : (
                    <span className="muted">–</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}

function HouseholdTab() {
  const { t } = useI18n();
  const { show } = useToast();
  const { data, setData } = useApi<{ name: string; settings: HouseholdSettings; can_edit: boolean; members: { id: number; display_name: string; role: string }[] }>("/household");
  const { run, busy } = useAction();
  const [saved, setSaved] = useState(false);

  if (!data) return <Loading />;
  const s = data.settings;
  const can = data.can_edit;
  const set = (patch: Partial<HouseholdSettings>) => {
    setSaved(false);
    setData({ ...data, settings: { ...s, ...patch } });
  };
  const save = () =>
    run(async () => {
      await api("/household", { method: "PUT", body: { name: data.name, settings: s } });
      setSaved(true);
    });

  return (
    <fieldset disabled={!can} style={{ border: 0, padding: 0, margin: 0, display: "grid", gap: 18 }}>
      {!can && <Notice>{t("settings.store.adminOnly")}</Notice>}
      <Card title={t("settings.tab.household")}>
        <Field label={t("settings.hh.name")}>
          <input value={data.name} onChange={(e) => setData({ ...data, name: e.target.value })} />
        </Field>
        <div className="form-grid">
          <NumberField label={`${t("settings.hh.defaults")}: ${t("common.adults")}`} value={s.default_adults} onChange={(v) => set({ default_adults: v ?? 0 })} />
          <NumberField label={`${t("settings.hh.defaults")}: ${t("common.children")}`} value={s.default_children} onChange={(v) => set({ default_children: v ?? 0 })} />
          <NumberField label={t("settings.hh.childPortion")} step={0.1} value={s.child_portion} onChange={(v) => set({ child_portion: v ?? 0.6 })} />
          <NumberField label={t("settings.hh.budgetWeek")} value={s.budget_weekly} onChange={(budget_weekly) => set({ budget_weekly })} />
          <NumberField label={t("settings.hh.budgetMonth")} value={s.budget_monthly} onChange={(budget_monthly) => set({ budget_monthly })} />
        </div>
        <div className="stack-sm">
          <span className="small strong muted">{t("settings.hh.mealTypes")}</span>
          <div className="row" style={{ gap: 14 }}>
            {MEALS.map((meal) => (
              <Check
                key={meal}
                label={t(`meal.${meal}` as TranslationKey)}
                checked={s.default_meal_types.includes(meal)}
                onChange={(on) => set({ default_meal_types: on ? [...s.default_meal_types, meal] : s.default_meal_types.filter((m) => m !== meal) })}
              />
            ))}
          </div>
        </div>
        <div className="stack-sm">
          <span className="small strong muted">{t("settings.hh.skipWeekdays")}</span>
          <div className="row" style={{ gap: 14 }}>
            {[0, 1, 2, 3, 4, 5, 6].map((day) => (
              <Check
                key={day}
                label={t(`weekday.${day}` as TranslationKey)}
                checked={s.skip_weekdays.includes(day)}
                onChange={(on) => set({ skip_weekdays: on ? [...s.skip_weekdays, day] : s.skip_weekdays.filter((d) => d !== day) })}
              />
            ))}
          </div>
        </div>
        <div className="form-grid">
          <NumberField label={t("settings.hh.repeat")} value={s.repeat_avoid_days} onChange={(v) => set({ repeat_avoid_days: v ?? 0 })} />
          <NumberField label={t("settings.hh.proteinLimit")} value={s.repeat_protein_per_week} onChange={(v) => set({ repeat_protein_per_week: v ?? 0 })} />
          <NumberField label={t("settings.hh.wasteDays")} value={s.waste_alert_days} onChange={(v) => set({ waste_alert_days: v ?? 0 })} />
          <Field label={t("settings.hh.voting")}>
            <select value={s.voting_rule} onChange={(e) => set({ voting_rule: e.target.value as HouseholdSettings["voting_rule"] })}>
              {(["single", "majority", "unanimous"] as const).map((rule) => (
                <option key={rule} value={rule}>
                  {t(`settings.hh.voting.${rule}` as TranslationKey)}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <Check label={t("settings.hh.trackConsumption")} hint={t("settings.hh.trackConsumptionHint")} checked={s.track_consumption} onChange={(track_consumption) => set({ track_consumption })} />
        <Check label={t("settings.hh.estimateExpiry")} hint={t("settings.hh.estimateExpiryHint")} checked={s.estimate_expiry} onChange={(estimate_expiry) => set({ estimate_expiry })} />
        <Check label={t("settings.hh.staples")} checked={s.assume_staples} onChange={(assume_staples) => set({ assume_staples })} />
        <Check label={t("settings.hh.builtinDishes")} hint={t("settings.hh.builtinDishesHint")} checked={s.builtin_dishes} onChange={(builtin_dishes) => set({ builtin_dishes })} />
      </Card>

      <Card title={t("settings.tab.food")}>
        <TagInput label={t("settings.allergies")} hint={t("settings.allergiesHint")} values={s.allergies} onChange={(allergies) => set({ allergies })} />
        <TagInput label={t("settings.dislikes")} hint={t("settings.dislikesHint")} values={s.dislikes} onChange={(dislikes) => set({ dislikes })} />
        <TagInput label={t("settings.likes")} hint={t("settings.likesHint")} values={s.likes} onChange={(likes) => set({ likes })} />
        <FrequencyInput values={s.frequencies ?? {}} onChange={(frequencies) => set({ frequencies })} />
        <TagInput label={t("settings.meat")} hint={t("settings.meatHint")} values={s.meat_preferences} onChange={(meat_preferences) => set({ meat_preferences })} />
        <div className="form-grid">
          <Field label={t("settings.diet")}>
            <select value={s.diet_mode ?? ""} onChange={(e) => set({ diet_mode: (e.target.value || null) as DietMode | null })}>
              <option value="">{t("diet.mode.none")}</option>
              {DIETS.map((diet) => (
                <option key={diet} value={diet}>
                  {t(`diet.mode.${diet}` as TranslationKey)}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <Check label={t("settings.dietCoach")} checked={s.diet_coach_enabled} onChange={(diet_coach_enabled) => set({ diet_coach_enabled })} />
      </Card>

      <Card title={t("settings.tab.stores")}>
        <div className="form-grid">
          <NumberField label={t("settings.hh.maxStores")} value={s.max_stores} onChange={(v) => set({ max_stores: v ?? 1 })} />
          <NumberField label={t("settings.hh.costPerKm")} value={s.cost_per_km} onChange={(v) => set({ cost_per_km: v ?? 0 })} />
          <NumberField label={t("settings.hh.costPerStop")} value={s.cost_per_extra_store} onChange={(v) => set({ cost_per_extra_store: v ?? 0 })} />
        </div>
        <div className="divider" />
        <h3>{t("settings.hh.home")}</h3>
        <div className="form-grid">
          <Field label={t("settings.hh.address")}>
            <input value={s.address ?? ""} onChange={(e) => set({ address: e.target.value || null })} />
          </Field>
          <NumberField label={t("settings.hh.lat")} value={s.lat} onChange={(lat) => set({ lat })} min={-90} />
          <NumberField label={t("settings.hh.lon")} value={s.lon} onChange={(lon) => set({ lon })} min={-180} />
        </div>
        <Check label={t("settings.hh.allowOsm")} hint={t("settings.hh.allowOsmHint")} checked={s.allow_osm_lookup} onChange={(allow_osm_lookup) => set({ allow_osm_lookup })} />
        <div className="row">
          <Button
            disabled={busy || !s.allow_osm_lookup || !s.address}
            onClick={() =>
              void run(async () => {
                await api("/household", { method: "PUT", body: { name: data.name, settings: s } });
                const found = await api<{ lat: number; lon: number }>("/household/geocode", { body: { address: s.address } });
                set({ lat: found.lat, lon: found.lon });
              })
            }
          >
            {t("settings.hh.geocode")}
          </Button>
          <Button
            disabled={busy || !s.allow_osm_lookup || s.lat == null || s.lon == null}
            onClick={() =>
              void run(async () => {
                await api("/household", { method: "PUT", body: { name: data.name, settings: s } });
                const result = await api<{ updated: number }>("/household/stores/nearby", { method: "POST" });
                show(t("settings.hh.foundStores", { count: result.updated }));
              })
            }
          >
            {t("settings.hh.findStores")}
          </Button>
        </div>
        <p className="small muted">
          <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">
            {t("settings.hh.osmCredit")}
          </a>
        </p>
      </Card>
      {can && <SaveBar busy={busy} saved={saved} onSave={() => void save()} />}
    </fieldset>
  );
}

function TokensTab() {
  const { t, locale } = useI18n();
  const { data: tokens, reload } = useApi<{ id: number; name: string; last_used_at: string | null }[]>("/me/tokens");
  const { run, busy } = useAction();
  const [name, setName] = useState("Home Assistant");
  const [created, setCreated] = useState<string | null>(null);

  return (
    <Card title={t("settings.tab.tokens")}>
      <p className="small muted">{t("settings.tokens.hint")}</p>
      <div className="row" style={{ alignItems: "flex-end" }}>
        <div className="grow" style={{ flexBasis: 200 }}>
          <Field label={t("settings.tokens.name")}>
            <input value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
        </div>
        <Button
          variant="primary"
          disabled={busy || !name.trim()}
          onClick={() =>
            void run(async () => {
              const result = await api<{ token: string }>("/me/tokens", { body: { name: name.trim() } });
              setCreated(result.token);
              await reload();
            })
          }
        >
          {t("settings.tokens.create")}
        </Button>
      </div>
      {created && (
        <Notice kind="good">
          {t("settings.tokens.created")}
          <div style={{ marginTop: 6 }}>
            <code style={{ overflowWrap: "anywhere" }}>{created}</code>
          </div>
        </Notice>
      )}
      {tokens && tokens.length === 0 && <p className="muted">{t("settings.tokens.empty")}</p>}
      <div>
        {tokens?.map((token) => (
          <div className="list-row" key={token.id}>
            <span className="grow">
              <Icon name="shield" size={15} /> {token.name}
              {token.last_used_at && <span className="small muted"> · {t("settings.tokens.lastUsed", { date: dateTime(token.last_used_at, locale) })}</span>}
            </span>
            <Button
              size="sm"
              variant="ghost"
              className="danger"
              onClick={() =>
                void run(async () => {
                  await api(`/me/tokens/${token.id}`, { method: "DELETE" });
                  await reload();
                })
              }
            >
              {t("common.delete")}
            </Button>
          </div>
        ))}
      </div>
    </Card>
  );
}
