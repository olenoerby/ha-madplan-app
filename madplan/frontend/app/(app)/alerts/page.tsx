"use client";

import { useState } from "react";
import { Button, Card, Check, Empty, Field, Icon, NumberField, PageHead } from "@/components/ui";
import { api } from "@/lib/api";
import { dayShort } from "@/lib/format";
import { useAction, useApi } from "@/lib/hooks";
import { useI18n, type TranslationKey } from "@/lib/i18n";
import type { Alert } from "@/lib/types";

const BASES: Alert["basis"][] = ["pack", "kg", "l", "stk"];

export default function AlertsPage() {
  const { t, locale } = useI18n();
  const { data: alerts, reload } = useApi<Alert[]>("/alerts");
  const { busy, run } = useAction();
  const [query, setQuery] = useState("");
  const [maxPrice, setMaxPrice] = useState<number | null>(null);
  const [basis, setBasis] = useState<Alert["basis"]>("pack");
  const [web, setWeb] = useState(false);

  const create = () =>
    run(async () => {
      await api("/alerts", { body: { query: query.trim(), max_price: maxPrice, basis, use_web_search: web } });
      setQuery("");
      setMaxPrice(null);
      setWeb(false);
      await reload();
    });

  const toggle = (alert: Alert, active: boolean) =>
    run(async () => {
      await api(`/alerts/${alert.id}`, {
        method: "PUT",
        body: { query: alert.query, max_price: alert.max_price, basis: alert.basis, use_web_search: alert.use_web_search, active },
      });
      await reload();
    });

  return (
    <>
      <PageHead title={t("alerts.title")} subtitle={t("alerts.subtitle")} />
      <Card title={t("alerts.new")}>
        <div className="form-grid">
          <Field label={t("alerts.query")}>
            <input value={query} placeholder={t("alerts.queryPlaceholder")} onChange={(e) => setQuery(e.target.value)} />
          </Field>
          <NumberField label={t("alerts.maxPrice")} value={maxPrice} onChange={setMaxPrice} />
          <Field label={t("alerts.basis")}>
            <select value={basis} onChange={(e) => setBasis(e.target.value as Alert["basis"])}>
              {BASES.map((b) => (
                <option key={b} value={b}>
                  {t(`alerts.basis.${b}` as TranslationKey)}
                </option>
              ))}
            </select>
          </Field>
        </div>
        <Check label={t("alerts.web")} hint={t("alerts.webHint")} checked={web} onChange={setWeb} />
        <div>
          <Button variant="primary" disabled={busy || query.trim().length < 2 || !maxPrice} onClick={() => void create()}>
            {t("common.add")}
          </Button>
        </div>
      </Card>

      <Card>
        {alerts && alerts.length === 0 && <Empty>{t("alerts.empty")}</Empty>}
        <div>
          {alerts?.map((alert) => (
            <div className="list-row" key={alert.id}>
              <div className="grow" style={{ flexBasis: 240 }}>
                <div className="strong">
                  {alert.query}{" "}
                  <span className="muted small">
                    {t("alerts.summary", { price: alert.max_price, basis: t(`alerts.basis.${alert.basis}` as TranslationKey) })}
                  </span>
                </div>
                {alert.last_match ? (
                  <div className="small">
                    <span className="badge good">
                      <Icon name="check" size={13} />
                      {t("alerts.match", { title: alert.last_match.title, price: alert.last_match.price, store: alert.last_match.store })}
                    </span>{" "}
                    {alert.last_match.valid_to && <span className="muted">{t("offers.until", { date: dayShort(alert.last_match.valid_to, locale) })} </span>}
                    {alert.last_match.url && (
                      <a href={alert.last_match.url} target="_blank" rel="noopener noreferrer nofollow">
                        {t("alerts.open")}
                      </a>
                    )}
                    {alert.last_match.source === "web" && <div className="muted">{t("alerts.matchWeb")}</div>}
                  </div>
                ) : (
                  <div className="small muted">{t("alerts.noMatch")}</div>
                )}
                {alert.use_web_search && (
                  <span className="badge" style={{ marginTop: 4 }}>
                    <Icon name="sparkle" size={13} />
                    {t("alerts.web")}
                  </span>
                )}
              </div>
              <Check label={t("alerts.active")} checked={alert.active} onChange={(value) => void toggle(alert, value)} />
              <Button
                size="sm"
                variant="ghost"
                className="danger"
                onClick={() =>
                  void run(async () => {
                    await api(`/alerts/${alert.id}`, { method: "DELETE" });
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
    </>
  );
}
