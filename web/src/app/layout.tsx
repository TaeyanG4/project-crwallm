import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "CRWALLM",
  description: "로컬 AI 크롤러",
};

/**
 * Two halves, and the order says what the tool is for: you describe what you
 * want in the chat, and you watch what the crawler did in the runs. Neither is
 * a mode - both stay reachable, because a crawl that goes wrong is diagnosed
 * in the run view and fixed in the chat.
 */
const NAV = [
  { href: "/chat", label: "대화" },
  { href: "/jobs", label: "크롤" },
  { href: "/recipes", label: "레시피" },
] as const;

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="ko" className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}>
      <body className="flex h-full min-h-0 flex-col">
        <header className="flex shrink-0 items-center gap-6 border-b px-4 py-2.5">
          <Link href="/jobs" className="font-semibold tracking-tight">
            CRWALLM
          </Link>
          <nav className="flex gap-1 text-sm">
            {NAV.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                className="rounded-md px-2.5 py-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
              >
                {item.label}
              </Link>
            ))}
          </nav>
        </header>
        <main className="min-h-0 flex-1 overflow-hidden">{children}</main>
      </body>
    </html>
  );
}
