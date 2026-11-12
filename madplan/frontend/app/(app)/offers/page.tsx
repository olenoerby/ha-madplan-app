"use client";

import { type FormEvent, type ReactNode, useState } from "react";
import { LineChart, StatTile } from "@/components/charts";
import { PlanDishButton } from "@/components/PlanDish";
import { Button, Card, Check, Empty, Icon, Loading, PageHead, Stepper } from "@/components/ui";
import { api } from "@/lib/api";
import { dayShort, kr, number, quantity } from "@/lib/format";
import { useAction, useApi } from "@/lib/hooks";
import { useI18n, type TranslationKey } from "@/lib/i18n";
import type { Offer, OfferDish, PriceIndex } from "@/lib/types";

const SOURCE_NAMES: Record<string, string> = { tilbudsugen: "Tilbudsugen", etilbudsavis: "eTilbudsavis" };

export default function OffersPage() {
  const { t } = useI18n();
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState<string | null>(null);
  const [allStores, setAllStores] = useState(false);
  const { data: tracked, reload: reloadTracked } = useApi<PriceIndex[]>("/offers/buy-now");

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (draft.trim().length >= 2) setQuery(draft.trim());
  };

  return (
    <>
      <PageHead title={t("offers.title")} subtitle={t("offers.subtitle")} />
      <Card>
        <form className="row" onSubmit={submit}>
          <input className="grow" style={{ flexBasis: 220 }} aria-label={t("offers.search")} placeholder={t("offers.placeholder")} value={draft} onChange={(e) => setDraft(e.target.value)} />
          <Button type="submit" variant="primary">
            {t("offers.searchButton")}
          </Button>
        </form>
        <Check label={t("offers.allStores")} checked={allStores} onChange={setAllStores} />
      </Card>

      {query && <QueryResult key={query} query={query} allStores={allStores} onChanged={reloadTracked} />}

      <Card title={t("index.myItems")}>
        {tracked && tracked.length === 0 && <p className="muted">{t("index.myItemsEmpty")}</p>}
        {tracked?.map((row) => (
          <div className="list-row" key={row.query}>
            <button type="button" className="meal-title grow" onClick={() => { setDraft(row.query); setQuery(row.query); }}>
              {row.query}
            </button>
            <VerdictBadge verdict={row.verdict} />
          </div>
        ))}
      </Card>
    </>
  );
}

function VerdictBadge({ verdict }: { verdict: PriceIndex["verdict"] }) {
  const { t } = useI18n();
  const kind = verdict === "buy" ? "good" : verdict === "wait" ? "warning" : "";
  return (
    <span className={`badge ${kind}`}>
      <Icon name={verdict === "buy" ? "check" : verdict === "wait" ? "alert" : "info"} size={14} />
      {t(`index.verdict.${verdict}` as TranslationKey)}
    </span>
  );
}

function QueryResult({ query, allStores, onChanged }: { query: string; allStores: boolean; onChanged: () => void }) {
  const { t, locale } = useI18n();
  const encoded = encodeURIComponent(query);
  const { data: index, reload } = useApi<PriceIndex>(`/offers/index?q=${encoded}`);
  const { data: offers, loading } = useApi<Offer[]>(`/offers/search?q=${encoded}&all_stores=${allStores}`);
  const { run } = useAction();

  const setPriority = (priority: number) =>
    run(async () => {
      await api("/me/items", { method: "PUT", body: { query, priority } });
      await reload();
      onChanged();
    });

  const basisLabel = index ? t(`index.basis.${index.basis}` as TranslationKey) : "";
  const money = (value: number) => kr(value, locale);

  return (
    <>
      {index && (
        <>
          <div className="grid-4">
            <StatTile label={t("index.current")} value={index.current_lowest != null ? money(index.current_lowest) : "–"} sub={index.current_store ? `${index.current_store} · ${basisLabel}` : basisLabel} />
            <StatTile
              label={t("index.typical")}
              value={index.typical_low != null ? money(index.typical_low) : "–"}
              sub={
                index.lowest_30_days != null
                  ? t("index.typicalSub30", { lowest: money(index.lowest ?? index.lowest_30_days), month: money(index.lowest_30_days) })
                  : index.lowest != null
                    ? t("index.typicalSub", { lowest: money(index.lowest) })
                    : basisLabel
              }
            />
            <StatTile label={t("index.average")} value={index.average != null ? money(index.average) : "–"} sub={basisLabel} />
            <div className="tile">
              <span className="tile-label">{t("index.buyNow")}</span>
              <span style={{ margin: "4px 0" }}>
                <VerdictBadge verdict={index.verdict} />
              </span>
              <span className="tile-sub">
                {index.enough_history
                  ? index.discount != null && index.discount > 0
                    ? t("index.discount", { percent: number(index.discount * 100, locale) })
                    : t("index.threshold", { percent: number(index.buy_threshold * 100, locale) })
                  : t("index.needMoreHistory", { points: index.history_points, days: index.history_days })}
              </span>
            </div>
          </div>

          <Card>
            <div className="row between">
              <div>
                <h3>{t("index.priority")}</h3>
                <p className="small muted">{t("index.priorityHint")}</p>
              </div>
              <Stepper label={t("index.priority")} value={index.priority} min={-3} max={3} onChange={(value) => void setPriority(value)} />
            </div>
          </Card>

          <Card>
            {index.history && index.history.length >= 2 ? (
              <LineChart
                title={t("index.history")}
                subtitle={t("index.historySub", { basis: basisLabel })}
                format={money}
                axisFormat={(v) => number(v, locale, v < 100 ? 1 : 0)}
                data={index.history.map((row) => ({ key: row.week, label: dayShort(row.week, locale), fullLabel: dayShort(row.week, locale), value: row.lowest }))}
                table={
                  <table>
                    <thead>
                      <tr>
                        <th>{t("budget.date")}</th>
                        <th className="num">{basisLabel}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {index.history.map((row) => (
                        <tr key={row.week}>
                          <td>{dayShort(row.week, locale)}</td>
                          <td className="num">{money(row.lowest)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                }
              />
            ) : (
              <p className="muted">{t("index.tooLittleData")}</p>
            )}
          </Card>
        </>
      )}

      <Card title={t("offers.results")}>
        {loading && !offers && <Loading />}
        {offers && offers.length === 0 && <Empty>{t("offers.noResults")}</Empty>}
        <div>
          {offers?.map((offer) => (
            <OfferRow key={offer.id} offer={offer}>
            <div className="list-row">
              <div className="grow" style={{ flexBasis: 220 }}>
                <div className="strong">{offer.title}</div>
                {offer.description && <div className="small muted">{offer.description}</div>}
                <div className="row small muted" style={{ gap: 6, marginTop: 2 }}>
                  <span>{offer.store}</span>
                  {offer.valid_to && <span>· {t("offers.until", { date: dayShort(offer.valid_to, locale) })}</span>}
                  {offer.pack_quantity && <span>· {quantity(offer.pack_quantity, offer.pack_unit, locale)}</span>}
                  {offer.url && (
                    <a href={offer.url} target="_blank" rel="noopener noreferrer nofollow">
                      {t("offers.fromSource", { source: SOURCE_NAMES[offer.source] ?? offer.source })}
                    </a>
                  )}
                </div>
              </div>
              <div className="right">
                <div className="num strong">{kr(offer.price, locale)}</div>
                {offer.unit_price != null && offer.unit_basis && (
                  <div className="num small muted">{t("common.perUnit", { price: number(offer.unit_price, locale, 2), unit: offer.unit_basis })}</div>
                )}
                {offer.pre_price != null && <div className="small muted">{t("offers.before", { price: kr(offer.pre_price, locale) })}</div>}
                <div className="row" style={{ justifyContent: "flex-end", gap: 4, marginTop: 2 }}>
                  <span className={`badge ${offer.kind === "offer" ? "good" : ""}`}>{t(`offers.kind.${offer.kind}` as TranslationKey)}</span>
                  {offer.membership_required && <span className="badge warning">{t("offers.member")}</span>}
                </div>
              </div>
            </div>
            </OfferRow>
          ))}
        </div>
      </Card>
    </>
  );
}

/** An offer, with the household's dishes that use it a click away. */
function OfferRow({ offer, children }: { offer: Offer; children: ReactNode }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  return (
    <div className="offer-row">
      {children}
      <div className="row no-print" style={{ justifyContent: "flex-end" }}>
        <Button size="sm" variant="ghost" aria-expanded={open} onClick={() => setOpen(!open)}>
          <Icon name="plan" size={14} />
          {open ? t("offers.dishes.hide") : t("offers.dishes.show")}
        </Button>
      </div>
      {open && <OfferDishes offerId={offer.id} />}
    </div>
  );
}

function OfferDishes({ offerId }: { offerId: number }) {
  const { t, locale } = useI18n();
  const { data: dishes, loading } = useApi<OfferDish[]>(`/offers/${offerId}/dishes`);
  if (loading && !dishes) return <Loading />;
  if (!dishes || dishes.length === 0) return <p className="small muted">{t("offers.dishes.none")}</p>;
  return (
    <div className="stack-sm offer-dishes">
      {dishes.map((dish) => (
        <div className="list-row" key={dish.id}>
          <span className="grow">
            {locale === "en" && dish.title_en ? dish.title_en : dish.title}
            <span className="small muted"> · {t("offers.dishes.cost", { cost: kr(dish.cost, locale), people: dish.people })}</span>
          </span>
          <PlanDishButton recipeId={dish.id} title={dish.title} mealTypes={dish.meal_types} />
        </div>
      ))}
    </div>
  );
}
