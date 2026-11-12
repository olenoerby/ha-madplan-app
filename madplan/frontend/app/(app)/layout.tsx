"use client";

import type { ReactNode } from "react";
import { Shell } from "@/components/Shell";
import { SessionProvider } from "@/lib/hooks";

export default function AppLayout({ children }: { children: ReactNode }) {
  return (
    <SessionProvider>
      <Shell>{children}</Shell>
    </SessionProvider>
  );
}
