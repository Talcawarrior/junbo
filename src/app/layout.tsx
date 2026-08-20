import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
import { Toaster } from "@/components/ui/toaster";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Junbo — Dashboard",
  description: "Polymarket Sicaklik Piyasasi Botu (PAPER) — spread + METAR-peak hibrit",
  openGraph: {
    title: "Junbo — Dashboard",
    description: "Polymarket Sicaklik Piyasasi Botu (PAPER) — spread + METAR-peak hibrit",
    type: "website",
  },
  twitter: {
    card: "summary",
    title: "Junbo — Dashboard",
    description: "Polymarket Sicaklik Piyasasi Botu (PAPER)",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased bg-background text-foreground`}
      >
        {children}
        <Toaster />
      </body>
    </html>
  );
}
