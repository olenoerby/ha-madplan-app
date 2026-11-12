"use client";

import { useState } from "react";
import { Button, Card, Check, Empty, Icon, Loading, Notice, PageHead, Segmented } from "@/components/ui";
import { api } from "@/lib/api";
import { dateTime, number } from "@/lib/format";
import { useAction, useApi, useMe, useToast } from "@/lib/hooks";
import { useI18n, type TranslationKey } from "@/lib/i18n";
import type { AdminUser, AppSetup, CrawlRun, Role, SourceRow } from "@/lib/types";

type Tab = "users" | "setup" | "sources";
const TABS: Tab[] = ["users", "setup", "sources"];

// What an admin can give. Administrators are Home Assistant's, and are made there.
const ROLES: Role[] = ["user", "household_manager", "guest"];

export default function AdminPage() {
  const { t } = useI18n();
  const me = useMe();
  const [tab, setTab] = useState<Tab>("users");
  if (me.role !== "admin") return <Notice kind="critical">{t("settings.store.adminOnly")}</Notice>;
  return (
    <>
      <PageHead title={t("admin.title")} />
      <div>
        <Segmented value={tab} onChange={setTab} options={TABS.map((value) => ({ value, label: t(`admin.tab.${value}` as TranslationKey) }))} />
      </div>
      {tab === "users" && <UsersTab />}
      {tab === "setup" && <SetupTab />}
      {tab === "sources" && <SourcesTab />}
    </>
  );
}

function UsersTab() {
  const { t, locale } = useI18n();
  const { data: users, reload } = useApi<AdminUser[]>("/admin/users");
  const { run, busy } = useAction();
  if (!users) return <Loading />;
  return (
    <Card title={t("admin.tab.users")}>
      <p className="small muted">{t("admin.users.fromHomeAssistant")}</p>
      {users.map((user) => (
        <div className="list-row" key={user.id}>
          <div className="grow" style={{ flexBasis: 240 }}>
            <div className="row">
              <span className="strong">{user.display_name}</span>
              <span className="small muted">{user.username}</span>
            </div>
            <div className="small muted">{user.last_login_at ? t("admin.users.lastLogin", { date: dateTime(user.last_login_at, locale) }) : t("admin.users.never")}</div>
          </div>
          {user.role_from_home_assistant ? (
            <span className="badge good">{t("admin.users.role.admin")}</span>
          ) : (
            <select
              aria-label={`${t("admin.users.role")}: ${user.display_name}`}
              style={{ width: "auto" }}
              value={user.role}
              disabled={busy}
              onChange={(e) =>
                void run(async () => {
                  await api(`/admin/users/${user.id}`, { method: "PATCH", body: { role: e.target.value } });
                  await reload();
                })
              }
            >
              {ROLES.map((value) => (
                <option key={value} value={value}>
                  {t(`admin.users.role.${value}` as TranslationKey)}
                </option>
              ))}
            </select>
          )}
        </div>
      ))}
    </Card>
  );
}

function SetupRow({ label, value, ok }: { label: string; value: string; ok?: boolean | null }) {
  return (
    <div className="list-row">
      <span className="grow">{label}</span>
      <span className="small">{value}</span>
      {ok !== undefined && ok !== null && <span className={`badge ${ok ? "good" : "critical"}`}><Icon name={ok ? "check" : "x"} size={13} /></span>}
    </div>
  );
}

function SetupTab() {
  const { t } = useI18n();
  const { data: setup } = useApi<AppSetup>("/admin/setup");
  const { run, busy } = useAction();
  const [results, setResults] = useState<Record<string, { ok: boolean; version?: string; error?: string; recipes?: number }> | null>(null);
  if (!setup) return <Loading />;
  const off = t("admin.setup.notSet");
  return (
    <>
      <Notice>{t("admin.setup.where")}</Notice>
      <Card title="Mealie">
        <SetupRow label={t("admin.setup.mealieUrl")} value={setup.mealie.url ?? off} ok={setup.mealie.configured} />
        <SetupRow label={t("admin.setup.mealieToken")} value={setup.mealie.token_set ? t("admin.setup.set") : off} />
        <SetupRow label={t("admin.setup.mealieList")} value={setup.mealie.shopping_list} />
        <p className="small muted">{setup.mealie.configured ? t("admin.setup.mealieOn") : t("admin.setup.mealieOff")}</p>
      </Card>
      <Card title={t("admin.setup.ai")}>
        <SetupRow label={t("admin.setup.aiEntity")} value={setup.ai.entity ?? off} ok={setup.ai.entity ? setup.ai.found : null} />
        {setup.ai.entity && setup.ai.found === false && <Notice kind="warning">{t("admin.setup.aiMissing")}</Notice>}
        <p className="small muted">{t("admin.setup.aiHint")}</p>
        {setup.ai.entity && <Notice kind="warning">{t("admin.setup.aiModelWarning")}</Notice>}
        {setup.ai.available.length === 0 ? (
          <p className="small muted">{t("admin.setup.aiNone")}</p>
        ) : (
          <div className="stack-sm">
            <span className="small strong muted">{t("admin.setup.aiAvailable")}</span>
            {setup.ai.available.map((entity) => (
              <div className="list-row" key={entity.entity_id}>
                <span className="grow">{entity.name}</span>
                <code className="small">{entity.entity_id}</code>
              </div>
            ))}
          </div>
        )}
      </Card>
      <Card title={t("admin.setup.other")}>
        <SetupRow label={t("admin.setup.notify")} value={setup.notify_service ?? off} />
        <SetupRow label={t("admin.setup.language")} value={setup.language === "da" ? "Dansk" : "English"} />
        <SetupRow label={t("admin.setup.autoPlan")} value={setup.schedule.auto_plan_enabled ? t("common.on") : t("common.off")} />
        <SetupRow label={t("admin.setup.crawl")} value={t("admin.setup.everyHours", { hours: setup.schedule.crawl_interval_hours })} />
      </Card>
      <Card>
        <div className="row">
          <Button
            disabled={busy}
            onClick={() =>
              void run(async () => setResults(await api<Record<string, { ok: boolean; version?: string; error?: string; recipes?: number }>>("/admin/setup/test", { method: "POST" })))
            }
          >
            {t("admin.setup.test")}
          </Button>
        </div>
        {results &&
          Object.entries(results).map(([name, result]) => (
            <SetupRow key={name} label={name === "mealie" ? "Mealie" : "Home Assistant"} value={
                result.ok
                  ? [result.version ?? "OK", result.recipes !== undefined ? t("admin.setup.recipes", { count: result.recipes }) : null].filter(Boolean).join(" · ")
                  : result.error === "not_configured"
                    ? off
                    : (result.error ?? "")
              } ok={result.ok} />
          ))}
      </Card>
    </>
  );
}

function SourcesTab() {
  const { t, locale } = useI18n();
  const { show } = useToast();
  const { data: sources, reload } = useApi<SourceRow[]>("/admin/sources");
  const { data: runs, reload: reloadRuns } = useApi<CrawlRun[]>("/admin/crawl/runs");
  const { run, busy } = useAction();

  if (!sources) return <Loading />;
  return (
    <>
      <Card
        title={t("admin.src.title")}
        actions={
          <Button
            variant="primary"
            disabled={busy}
            onClick={() =>
              void run(async () => {
                await api("/admin/crawl", { method: "POST" });
                show(t("admin.src.queued"));
              })
            }
          >
            {t("admin.src.crawl")}
          </Button>
        }
      >
        <p className="small muted">{t("admin.src.hint")}</p>
        <div>
          {sources.map((source) => (
            <div className="list-row" key={source.slug} style={{ alignItems: "flex-start" }}>
              <div className="grow" style={{ flexBasis: 280 }}>
                <div className="row">
                  <a className="strong" href={source.homepage} target="_blank" rel="noopener noreferrer">
                    {source.name}
                  </a>
                  <span className={`badge ${source.policy === "allowed" ? "good" : "critical"}`}>
                    <Icon name={source.policy === "allowed" ? "check" : "x"} size={13} />
                    {t(source.policy === "allowed" ? "admin.src.allowed" : "admin.src.blocked")}
                  </span>
                  {source.policy === "allowed" && <span className="small muted">{t("admin.src.offers", { count: number(source.offers, locale) })}</span>}
                </div>
                <p className="small muted">{t(`admin.src.note.${source.slug}` as TranslationKey) === `admin.src.note.${source.slug}` ? source.note : t(`admin.src.note.${source.slug}` as TranslationKey)}</p>
                <p className="small muted">
                  {t("admin.src.checked", { date: source.terms_checked_on })}
                  {source.policy === "allowed" && ` · ${source.last_run_at ? t("admin.src.lastRun", { date: dateTime(source.last_run_at, locale) }) : t("admin.src.neverRun")}`}
                </p>
              </div>
              {source.can_enable && (
                <Check
                  label={t("common.on")}
                  checked={source.enabled}
                  onChange={(enabled) =>
                    void run(async () => {
                      await api(`/admin/sources/${source.slug}`, { method: "PATCH", body: { enabled } });
                      await reload();
                    })
                  }
                />
              )}
            </div>
          ))}
        </div>
      </Card>
      <Card title={t("admin.src.runs")} actions={<Button size="sm" variant="ghost" onClick={() => void reloadRuns()}>{t("common.refresh")}</Button>}>
        {runs && runs.length === 0 && <Empty>{t("admin.src.neverRun")}</Empty>}
        {runs && runs.length > 0 && (
          <div className="table-wrap">
            <table>
              <tbody>
                {runs.map((row) => (
                  <tr key={row.id}>
                    <td className="nowrap">{dateTime(row.started_at, locale)}</td>
                    <td>{row.source}</td>
                    <td>
                      <span className={`badge ${row.status === "ok" ? "good" : row.status === "running" ? "" : row.status === "partial" ? "warning" : "critical"}`}>
                        {t(`admin.src.status.${row.status}` as TranslationKey)}
                      </span>
                    </td>
                    <td className="num">{t("admin.src.requests", { count: row.requests })}</td>
                    <td className="num">{t("admin.src.seen", { count: number(row.offers_seen, locale) })}</td>
                    <td className="small muted">{row.error?.split("\n")[0]}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}
