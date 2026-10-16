import type { Metadata } from "next";
import "./globals.css";
import { AuthProvider } from "../components/auth-provider";
export const metadata: Metadata = { title: "Northstar | Integration Platform", description: "Staffing operations integration foundation" };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body><AuthProvider>{children}</AuthProvider></body></html>;
}
