"use client";

import { type ReactNode, useEffect } from "react";
import { ToastProvider } from "@/lib/hooks";
import { I18nProvider } from "@/lib/i18n";
import { registerServiceWorker } from "@/lib/offline";

export function Providers({ children }: { children: ReactNode }) {
  useEffect(() => registerServiceWorker(), []);
  return (
    <I18nProvider>
      <ToastProvider>{children}</ToastProvider>
    </I18nProvider>
  );
}
