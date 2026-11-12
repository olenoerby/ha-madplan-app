"use client";

import { type ChangeEvent, useRef, useState } from "react";
import { Button, Card, Check, Empty, Field, Icon, Modal, Notice, PageHead } from "@/components/ui";
import { api } from "@/lib/api";
import { dayShort, kr, quantity, todayIso, parseDay } from "@/lib/format";
import { useAction, useApi, useMe, useToast } from "@/lib/hooks";
import { useI18n, type TranslationKey } from "@/lib/i18n";
import type { DishSuggestion, PantryItem, RestockSuggestion, WasteData } from "@/lib/types";

function daysLeft(iso: string) {
  return Math.round((parseDay(iso).getTime() - parseDay(todayIso()).getTime()) / 86_400_000);
}

export default function PantryPage() {
  const { t, locale } = useI18n();
  const me = useMe();
  const { show } = useToast();
  const { data: items, reload } = useApi<PantryItem[]>("/pantry");
  const { data: waste, reload: reloadWaste } = useApi<WasteData>(me.settings.features.waste_agent ? "/waste" : null);
  const { data: restock, reload: reloadRestock } = useApi<RestockSuggestion[]>(me.settings.features.restock ? "/pantry/restock" : null);
  const { busy, run } = useAction();
  const [name, setName] = useState("");
  const [amount, setAmount] = useState("");
  const [unit, setUnit] = useState("");
  const [location, setLocation] = useState("");
  const [expires, setExpires] = useState("");
  const [minimum, setMinimum] = useState("");
  const [editing, setEditing] = useState<PantryItem | null>(null);
  const [scanning, setScanning] = useState(false);
  const [scanMessage, setScanMessage] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const refresh = async () => {
    await reload();
    await reloadWaste();
    await reloadRestock();
  };

  const add = () =>
    run(async () => {
      await api("/pantry", {
        body: { name: name.trim(), quantity: amount === "" ? null : Number(amount), unit: unit.trim() || null, location: location.trim() || null, expires_on: expires || null, min_quantity: minimum === "" ? null : Number(minimum) },
      });
      setName("");
      setAmount("");
      setUnit("");
      setExpires("");
      setMinimum("");
      await refresh();
    });

  const scan = async (event: ChangeEvent<HTMLInputElement>) => {
    const files = event.target.files;
    if (!files || files.length === 0) return;
    const form = new FormData();
    for (const file of Array.from(files)) form.append("photos", file);
    setScanning(true);
    setScanMessage(null);
    await run(async () => {
      const result = await api<{ found: unknown[]; applied: unknown[] }>("/pantry/scan", { form });
      setScanMessage(result.found.length ? t("pantry.photoFound", { count: result.found.length }) : t("pantry.photoNoneFound"));
      await refresh();
    });
    setScanning(false);
    event.target.value = "";
  };

  const locations = new Map<string, PantryItem[]>();
  // The database puts capitals first, so that "Toiletpapir" would come before "agurk".
  for (const item of [...(items ?? [])].sort((a, b) => a.name.localeCompare(b.name, locale))) {
    const key = item.location || t("pantry.title");
    locations.set(key, [...(locations.get(key) ?? []), item]);
  }

  return (
    <>
      <PageHead title={t("pantry.title")} subtitle={t("pantry.subtitle")} />

      {me.settings.features.waste_agent && waste && (
        <Card title={t("waste.title")}>
          {waste.items.length === 0 ? (
            <p className="muted">{t("waste.none")}</p>
          ) : (
            <>
              <div className="tags">
                {waste.items.map((item) => (
                  <span key={item.id} className={`badge ${item.days_left <= 1 ? "critical" : "warning"}`}>
                    {item.name} ·{" "}
                    {item.estimated && item.days_left >= 0 ? `${t("pantry.about")} ` : ""}
                    {item.days_left < 0 ? t("pantry.expired") : item.days_left === 0 ? t("pantry.expiresToday") : item.days_left === 1 ? t("pantry.dayLeft") : t("pantry.daysLeft", { days: item.days_left })}
                  </span>
                ))}
              </div>
              {waste.dishes.length > 0 && (
                <div>
                  <h3>{t("waste.dishes")}</h3>
                  {waste.dishes.map((dish) => (
                    <div className="list-row" key={dish.recipe_id}>
                      <span className="grow strong">{locale === "en" && dish.title_en ? dish.title_en : dish.title}</span>
                      <span className="small muted">{t("waste.uses", { items: dish.uses.join(", ") })}</span>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </Card>
      )}

      {me.settings.features.restock && restock && restock.length > 0 && (
        <Card title={t("restock.title")}>
          <div>
            {restock.map((suggestion) => (
              <div className="list-row" key={suggestion.key}>
                <span className="grow">
                  <span className="strong">{suggestion.name}</span>{" "}
                  <span className="muted small">
                    {suggestion.reason === "below_minimum"
                      ? t("restock.below", { have: quantity(suggestion.quantity ?? 0, suggestion.unit ?? null, locale) || "0" })
                      : t("restock.every", { days: suggestion.every_days ?? 0 })}
                  </span>
                  {suggestion.offer && (
                    <span className="small"> · {t("restock.offer", { price: kr(suggestion.offer.price, locale), store: suggestion.offer.store })}</span>
                  )}
                </span>
                <Button size="sm" disabled={busy} onClick={() => void run(async () => {
                  const added = await api<{ allergy_warning: string | null }>("/pantry/restock/add", { body: { name: suggestion.name } });
                  show(added.allergy_warning ? t("restock.addedWarning", { allergy: added.allergy_warning }) : t("restock.added"));
                  await refresh();
                })}>
                  {t("restock.add")}
                </Button>
                <Button size="sm" variant="ghost" disabled={busy} onClick={() => void run(async () => {
                  await api("/pantry/restock/snooze", { body: { key: suggestion.key } });
                  await refresh();
                })}>
                  {t("restock.notNow")}
                </Button>
              </div>
            ))}
          </div>
        </Card>
      )}

      <div className="grid-2">
        <Card title={t("pantry.add")}>
          <div className="form-grid">
            <Field label={t("pantry.name")}>
              <input value={name} onChange={(e) => setName(e.target.value)} />
            </Field>
            <Field label={t("pantry.quantity")}>
              <input type="number" inputMode="decimal" min={0} value={amount} onChange={(e) => setAmount(e.target.value)} />
            </Field>
            <Field label={t("pantry.unit")}>
              <input value={unit} placeholder="g, ml, stk" onChange={(e) => setUnit(e.target.value)} />
            </Field>
            <Field label={t("pantry.location")}>
              <input value={location} placeholder={t("pantry.locationPlaceholder")} onChange={(e) => setLocation(e.target.value)} />
            </Field>
            <Field label={t("pantry.expires")}>
              <input type="date" value={expires} onChange={(e) => setExpires(e.target.value)} />
            </Field>
            <Field label={t("pantry.minimum")} hint={t("pantry.minimumHint")}>
              <input type="number" inputMode="decimal" min={0} value={minimum} onChange={(e) => setMinimum(e.target.value)} />
            </Field>
          </div>
          <div>
            <Button variant="primary" disabled={busy || !name.trim()} onClick={() => void add()}>
              {t("common.add")}
            </Button>
          </div>
        </Card>

        {me.settings.features.pantry_photo && (
          <Card title={t("pantry.photo")}>
            <p className="small muted">{t("pantry.photoHint")}</p>
            <input ref={fileInput} type="file" accept="image/jpeg,image/png,image/webp" capture="environment" multiple hidden onChange={(e) => void scan(e)} />
            <div>
              <Button working={scanning} workingTexts={[t("working.photo1"), t("working.photo2"), t("working.longer")]} onClick={() => fileInput.current?.click()}>
                <Icon name="camera" />
                {t("pantry.photoChoose")}
              </Button>
            </div>
            {scanMessage && <Notice kind="good">{scanMessage}</Notice>}
          </Card>
        )}
      </div>

      <CookCard />

      {items && items.length === 0 && (
        <Card>
          <Empty>{t("pantry.empty")}</Empty>
        </Card>
      )}
      {[...locations.entries()].map(([place, rows]) => (
        <Card key={place} title={place}>
          <div>
            {rows.map((item) => {
              const left = item.expires_on ? daysLeft(item.expires_on) : null;
              return (
                <div className="list-row" key={item.id}>
                  <span className="grow">
                    <span className="strong">{item.name}</span>{" "}
                    <span className="muted small">{item.quantity === 0 ? t("pantry.empty.item") : quantity(item.quantity, item.unit, locale)}</span>
                    {item.source !== "manual" && <span className="muted small"> · {t(`pantry.source.${item.source}` as TranslationKey)}</span>}
                  </span>
                  {item.expires_on && (
                    <span className={`badge ${left !== null && left <= 1 ? "critical" : left !== null && left <= 3 ? "warning" : ""}`} title={item.expires_estimated ? t("pantry.estimatedHint") : undefined}>
                      {item.expires_estimated ? `${t("pantry.about")} ` : ""}
                      {dayShort(item.expires_on, locale)}
                    </span>
                  )}
                  <Button size="sm" variant="ghost" onClick={() => setEditing(item)}>
                    {t("common.edit")}
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    className="danger"
                    onClick={() =>
                      void run(async () => {
                        await api(`/pantry/${item.id}`, { method: "DELETE" });
                        await refresh();
                      })
                    }
                  >
                    {t("common.delete")}
                  </Button>
                </div>
              );
            })}
          </div>
        </Card>
      ))}
      {editing && (
        <EditItem
          item={editing}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null);
            await refresh();
          }}
        />
      )}
    </>
  );
}

function EditItem({ item, onClose, onSaved }: { item: PantryItem; onClose: () => void; onSaved: () => Promise<void> }) {
  const { t } = useI18n();
  const { busy, run } = useAction();
  const [name, setName] = useState(item.name);
  const [amount, setAmount] = useState(item.quantity?.toString() ?? "");
  const [unit, setUnit] = useState(item.unit ?? "");
  const [location, setLocation] = useState(item.location ?? "");
  const [expires, setExpires] = useState(item.expires_on ?? "");
  const [minimum, setMinimum] = useState(item.min_quantity?.toString() ?? "");
  return (
    <Modal title={item.name} onClose={onClose}>
      <div className="form-grid">
        <Field label={t("pantry.name")}>
          <input value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label={t("pantry.quantity")}>
          <input type="number" inputMode="decimal" min={0} value={amount} onChange={(e) => setAmount(e.target.value)} />
        </Field>
        <Field label={t("pantry.unit")}>
          <input value={unit} placeholder="g, ml, stk" onChange={(e) => setUnit(e.target.value)} />
        </Field>
        <Field label={t("pantry.location")}>
          <input value={location} placeholder={t("pantry.locationPlaceholder")} onChange={(e) => setLocation(e.target.value)} />
        </Field>
        <Field label={t("pantry.expires")} hint={item.expires_estimated ? t("pantry.estimatedHint") : undefined}>
          <input type="date" value={expires} onChange={(e) => setExpires(e.target.value)} />
        </Field>
        <Field label={t("pantry.minimum")} hint={t("pantry.minimumHint")}>
          <input type="number" inputMode="decimal" min={0} value={minimum} onChange={(e) => setMinimum(e.target.value)} />
        </Field>
      </div>
      <div>
        <Button
          variant="primary"
          disabled={busy || !name.trim()}
          onClick={() =>
            void run(async () => {
              await api(`/pantry/${item.id}`, {
                method: "PUT",
                body: { name: name.trim(), quantity: amount === "" ? null : Number(amount), unit: unit.trim() || null, location: location.trim() || null, expires_on: expires || null, min_quantity: minimum === "" ? null : Number(minimum) },
              });
              await onSaved();
            })
          }
        >
          {t("common.save")}
        </Button>
      </div>
    </Modal>
  );
}

function CookCard() {
  const { t, locale } = useI18n();
  const { busy, run } = useAction();
  const [draft, setDraft] = useState("");
  const [useAll, setUseAll] = useState(false);
  const [results, setResults] = useState<DishSuggestion[] | null>(null);

  const search = () =>
    run(async () => {
      const ingredients = draft.split(/,|\bog\b|\band\b/i).map((part) => part.trim()).filter(Boolean);
      setResults(await api<DishSuggestion[]>("/pantry/suggest", { body: { ingredients, use_pantry: useAll } }));
    });

  return (
    <Card title={t("cook.title")}>
      <form
        className="row"
        onSubmit={(e) => {
          e.preventDefault();
          void search();
        }}
      >
        <input className="grow" style={{ flexBasis: 240 }} aria-label={t("cook.title")} placeholder={t("cook.placeholder")} value={draft} onChange={(e) => setDraft(e.target.value)} />
        <Button type="submit" disabled={busy || (!draft.trim() && !useAll)}>
          {t("cook.ask")}
        </Button>
      </form>
      <Check label={t("cook.useAll")} checked={useAll} onChange={setUseAll} />
      {results && results.length === 0 && <p className="muted">{t("cook.none")}</p>}
      {results && results.length > 0 && (
        <div>
          {results.map((dish) => (
            <div className="list-row" key={dish.recipe_id}>
              <span className="grow strong">{locale === "en" && dish.title_en ? dish.title_en : dish.title}</span>
              <span className="small muted">{dish.missing.length ? t("cook.missing", { items: dish.missing.join(", ") }) : t("cook.haveAll")}</span>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}
