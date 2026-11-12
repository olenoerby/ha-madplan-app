"use client";

import Image from "next/image";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState, type ReactNode } from "react";
import { Button, Field, Icon, Modal, Notice } from "@/components/ui";
import { BASE } from "@/lib/api";
import { useAction, useApi, useMe, useSession, useToast } from "@/lib/hooks";
import { type TranslationKey, useI18n } from "@/lib/i18n";
import type { FeatureToggles, Me, Notification } from "@/lib/types";

interface NavItem {
  href: string;
  label: TranslationKey;
  icon: string;
  feature?: keyof FeatureToggles;
  adminOnly?: boolean;
}

const NAV: NavItem[] = [
  { href: "/", label: "nav.home", icon: "home" },
  { href: "/plan", label: "nav.plan", icon: "plan" },
  { href: "/recipes", label: "nav.recipes", icon: "book", feature: "new_recipes" },
  { href: "/shopping", label: "nav.shopping", icon: "cart" },
  { href: "/offers", label: "nav.offers", icon: "tag" },
  { href: "/alerts", label: "nav.alerts", icon: "bell", feature: "price_alerts" },
  { href: "/pantry", label: "nav.pantry", icon: "pantry" },
  { href: "/assistant", label: "nav.assistant", icon: "chat", feature: "assistant" },
  { href: "/budget", label: "nav.budget", icon: "budget" },
  { href: "/calendar", label: "nav.calendar", icon: "calendar" },
  { href: "/diet", label: "nav.diet", icon: "leaf", feature: "diet_coach" },
  { href: "/settings", label: "nav.settings", icon: "settings" },
  { href: "/admin", label: "nav.admin", icon: "shield", adminOnly: true },
];

const PHONE_TABS = ["/", "/plan", "/shopping", "/assistant"];

function visibleItems(me: Me): NavItem[] {
  // A guest may look and vote. The assistant spends the household's AI budget, so it is not theirs.
  const guestHidden = me.role === "guest" ? ["/assistant", "/alerts"] : [];
  return NAV.filter(
    (item) => !guestHidden.includes(item.href) && (item.adminOnly ? me.role === "admin" : !item.feature || me.settings.features[item.feature]),
  );
}

function isCurrent(path: string, href: string) {
  return href === "/" ? path === "/" : path === href || path.startsWith(`${href}/`);
}

export function Shell({ children }: { children: ReactNode }) {
  const { me, loading, outside, logout } = useSession();
  const { t } = useI18n();
  if (outside) {
    return (
      <main className="main">
        <div className="main-inner">
          <Notice>
            <strong>{t("outside.title")}</strong>
            <br />
            {t("outside.hint")}
          </Notice>
        </div>
      </main>
    );
  }
  if (loading || !me) return null;
  return (
    <Authenticated me={me} logout={logout}>
      {children}
    </Authenticated>
  );
}

function Authenticated({ me, logout, children }: { me: Me; logout: () => Promise<void>; children: ReactNode }) {
  const { t } = useI18n();
  const path = usePathname();
  const [menuOpen, setMenuOpen] = useState(false);
  const { data: unread, reload } = useApi<Notification[]>("/notifications?unread_only=true");
  const items = visibleItems(me);

  useEffect(() => {
    setMenuOpen(false);
    void reload();
  }, [path, reload]);

  const tabs = items.filter((i) => PHONE_TABS.includes(i.href));
  const rest = items.filter((i) => !PHONE_TABS.includes(i.href));

  return (
    <div className="shell">
      <nav className="sidebar" aria-label="MadPlan">
        <Link href="/" className="brand">
          <Image src={`${BASE}/icon-192.png`} alt="" width={28} height={28} />
          MadPlan
        </Link>
        {items.map((item) => (
          <Link key={item.href} href={item.href} className="nav-link" aria-current={isCurrent(path, item.href) ? "page" : undefined}>
            <Icon name={item.icon} />
            {t(item.label)}
            {item.href === "/" && unread && unread.length > 0 && <span className="nav-count">{unread.length}</span>}
          </Link>
        ))}
        <div className="nav-spacer" />
        <div className="small muted" style={{ padding: "8px 10px 2px" }}>
          {me.display_name}
        </div>
      </nav>

      <main className="main">
        <div className="main-inner">
          <OfflineBanner />
          {children}
        </div>
      </main>

      {menuOpen && (
        <div className="more-menu" role="menu">
          {rest.map((item) => (
            <Link key={item.href} href={item.href} className="nav-link" role="menuitem" aria-current={isCurrent(path, item.href) ? "page" : undefined}>
              <Icon name={item.icon} />
              {t(item.label)}
            </Link>
          ))}
          <button type="button" className="nav-link btn ghost" style={{ justifyContent: "flex-start" }} role="menuitem" onClick={() => void logout()}>
            {t("nav.logout")}
          </button>
        </div>
      )}

      <nav className="bottom-nav" aria-label="MadPlan">
        {tabs.map((item) => (
          <Link key={item.href} href={item.href} aria-current={isCurrent(path, item.href) ? "page" : undefined}>
            <Icon name={item.icon} size={21} />
            {t(item.label)}
          </Link>
        ))}
        <button type="button" aria-expanded={menuOpen} onClick={() => setMenuOpen((open) => !open)}>
          <Icon name="more" size={21} />
          {t("nav.more")}
        </button>
      </nav>
    </div>
  );
}

/** Says so when there is no network, as pages then show the last copy they kept. */
function OfflineBanner() {
  const { t } = useI18n();
  const [offline, setOffline] = useState(false);
  useEffect(() => {
    const update = () => setOffline(!navigator.onLine);
    update();
    window.addEventListener("online", update);
    window.addEventListener("offline", update);
    return () => {
      window.removeEventListener("online", update);
      window.removeEventListener("offline", update);
    };
  }, []);
  if (!offline) return null;
  return (
    <div className="offline-banner" role="status">
      {t("offline.banner")}
    </div>
  );
}
