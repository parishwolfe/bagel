import type { ReactNode } from "react";
import "./style.css";

export const metadata = { title: "Bagel • Local operator", description: "Local research and paper readiness" };

export default function Layout({ children }: { children: ReactNode }) {
  return <html lang="en"><body>{children}</body></html>;
}
