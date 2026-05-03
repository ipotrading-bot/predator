// ui/app/layout.tsx — Layout racine Next.js 14
import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import Sidebar from "@/components/Sidebar";

const inter = Inter({ subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Predator PAIM v2.0",
  description: "Algorithmic Information Arbitrage Dashboard",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="fr" className="dark">
      <body className={`${inter.className} bg-[#030712] text-gray-100 min-h-screen`}>
        <div className="flex">
          <Sidebar />
          <main className="flex-1 ml-56 min-h-screen p-6">{children}</main>
        </div>
      </body>
    </html>
  );
}
