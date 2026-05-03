"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";

const NAV = [
  { href: "/",        icon: "🖥️",  label: "Terminal"  },
  { href: "/signals", icon: "⚡",  label: "Signals 7/9" },
  { href: "/ledger",  icon: "📋",  label: "Ledger"    },
  { href: "/audit",   icon: "📊",  label: "Audit"     },
];

export default function Sidebar() {
  const path = usePathname();
  return (
    <aside className="fixed left-0 top-0 h-screen w-56 bg-gray-950 border-r border-gray-800 flex flex-col z-10">
      {/* Logo */}
      <div className="px-5 py-5 border-b border-gray-800">
        <p className="text-lg font-bold tracking-tight">🦅 PREDATOR</p>
        <p className="text-xs text-gray-600 font-mono">PAIM v2.0</p>
      </div>

      {/* Nav */}
      <nav className="flex-1 px-3 py-4 space-y-1">
        {NAV.map((item) => {
          const active = path === item.href;
          return (
            <Link
              key={item.href}
              href={item.href}
              className={`flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm transition-all ${
                active
                  ? "bg-emerald-900/40 text-emerald-300 border border-emerald-800"
                  : "text-gray-400 hover:text-gray-200 hover:bg-gray-800/50"
              }`}
            >
              <span>{item.icon}</span>
              <span className="font-medium">{item.label}</span>
            </Link>
          );
        })}
      </nav>

      {/* Footer */}
      <div className="px-5 py-4 border-t border-gray-800">
        <p className="text-xs text-gray-600">Dakar, Sénégal</p>
        <p className="text-xs text-gray-700 font-mono">UTC+0</p>
      </div>
    </aside>
  );
}
