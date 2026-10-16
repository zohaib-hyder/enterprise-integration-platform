"use client";
import { AuthGuard, useAuth, UserControls } from "./auth-provider";
import Dashboard from "./dashboard";
export default function Workspace({ children, section = "Overview" }: { children?: React.ReactNode; section?: string }) {
  const { me } = useAuth();
  return <AuthGuard><a href="#main-content" className="sr-only focus:not-sr-only focus:fixed focus:left-4 focus:top-4 focus:z-50 focus:rounded focus:bg-white focus:p-3">Skip to main content</a><UserControls /><Dashboard section={section} permissions={me?.permissions ?? []} content={children ? <div key={me?.active_tenant_id ?? "none"}>{children}</div> : undefined} /></AuthGuard>;
}
