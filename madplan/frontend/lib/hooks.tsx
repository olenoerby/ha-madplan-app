"use client";

import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError, api, homeAssistantLanguage } from "./api";
import { clearOfflineData } from "./offline";
import { errorText, useI18n, type Locale } from "./i18n";
import type { Me } from "./types";

/** Fetches a path. A reload keeps the previous data on screen, so the page never flashes. */
export function useApi<T>(path: string | null) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(path !== null);
  const latest = useRef(0);

  const reload = useCallback(async () => {
    if (path === null) return;
    const ticket = ++latest.current;
    setLoading(true);
    try {
      const result = await api<T>(path);
      if (ticket === latest.current) {
        setData(result);
        setError(null);
      }
    } catch (caught) {
      if (ticket === latest.current) setError(caught as ApiError);
    } finally {
      if (ticket === latest.current) setLoading(false);
    }
  }, [path]);

  useEffect(() => {
    if (path === null) {
      setData(null);
      setLoading(false);
      return;
    }
    void reload();
  }, [path, reload]);

  return { data, setData, error, loading, reload };
}

interface Toasts {
  show: (message: string) => void;
}

const ToastContext = createContext<Toasts>({ show: () => undefined });

export function ToastProvider({ children }: { children: ReactNode }) {
  const [messages, setMessages] = useState<{ id: number; text: string }[]>([]);
  const counter = useRef(0);
  const show = useCallback((text: string) => {
    const id = ++counter.current;
    setMessages((current) => [...current, { id, text }]);
    setTimeout(() => setMessages((current) => current.filter((m) => m.id !== id)), 5000);
  }, []);
  return (
    <ToastContext.Provider value={{ show }}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {messages.map((m) => (
          <div className="toast" key={m.id}>
            {m.text}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  return useContext(ToastContext);
}

/** Wraps an action: tracks that it is running, and shows any failure as a message. */
export function useAction() {
  const { t } = useI18n();
  const { show } = useToast();
  const [busy, setBusy] = useState(false);
  const run = useCallback(
    async <R,>(action: () => Promise<R>): Promise<R | undefined> => {
      setBusy(true);
      try {
        return await action();
      } catch (caught) {
        show(errorText(t, caught));
        return undefined;
      } finally {
        setBusy(false);
      }
    },
    [show, t],
  );
  return { busy, run };
}

interface Session {
  me: Me | null;
  loading: boolean;
  /** Opened outside Home Assistant, which is the only way in. */
  outside: boolean;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
}

const SessionContext = createContext<Session | null>(null);

/** The person's own language in Home Assistant, as MadPlan's: Danish for Danish, English otherwise. */
function languageInHomeAssistant(): Locale | null {
  const language = homeAssistantLanguage();
  return language ? (language.toLowerCase().startsWith("da") ? "da" : "en") : null;
}

// How often to look for a change of language in Home Assistant. It is only a local read.
const LANGUAGE_CHECK_MS = 2000;

export function SessionProvider({ children }: { children: ReactNode }) {
  const { setLocale } = useI18n();
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);
  const [outside, setOutside] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const user = await api<Me>("/auth/me");
      setMe(user);
      setLocale(user.locale);
    } catch (caught) {
      setMe(null);
      // Only possible when the app is opened outside Home Assistant, which does the logging in.
      if (caught instanceof ApiError && caught.status === 401) setOutside(true);
    } finally {
      setLoading(false);
    }
  }, [setLocale]);

  // Logging out is Home Assistant's. This only forgets what was kept on this device.
  const logout = useCallback(async () => {
    clearOfflineData();
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Follows the person's language in Home Assistant at once, also when they change it while
  // MadPlan is open, and tells the server, so messages come in the same language.
  const seenLanguage = useRef<Locale | null>(null);
  useEffect(() => {
    const check = () => {
      const language = languageInHomeAssistant();
      if (!language || language === seenLanguage.current) return;
      const first = seenLanguage.current === null;
      seenLanguage.current = language;
      setLocale(language);
      if (!first) void refresh();
    };
    check();
    const timer = window.setInterval(check, LANGUAGE_CHECK_MS);
    return () => window.clearInterval(timer);
  }, [refresh, setLocale]);

  return <SessionContext.Provider value={{ me, loading, outside, refresh, logout }}>{children}</SessionContext.Provider>;
}

export function useSession(): Session {
  const context = useContext(SessionContext);
  if (!context) throw new Error("useSession outside SessionProvider");
  return context;
}

export function useMe(): Me {
  const { me } = useSession();
  if (!me) throw new Error("useMe before the session is loaded");
  return me;
}
