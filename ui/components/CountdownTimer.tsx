"use client";
import { useEffect, useState } from "react";

export default function CountdownTimer() {
  const [remain, setRemain] = useState("");

  useEffect(() => {
    const tick = () => {
      const now = new Date();
      const h = now.getUTCHours();
      const nextHour = h < 8 ? 8 : h < 16 ? 16 : 24;
      const next = new Date(now);
      next.setUTCHours(nextHour % 24, 0, 0, 0);
      if (nextHour === 24) next.setUTCDate(next.getUTCDate() + 1);
      const diff = next.getTime() - now.getTime();
      const hh = Math.floor(diff / 3_600_000);
      const mm = Math.floor((diff % 3_600_000) / 60_000);
      const ss = Math.floor((diff % 60_000) / 1_000);
      setRemain(`${String(hh).padStart(2,"0")}:${String(mm).padStart(2,"0")}:${String(ss).padStart(2,"0")}`);
    };
    tick();
    const iv = setInterval(tick, 1000);
    return () => clearInterval(iv);
  }, []);

  const sessions = [
    { name: "Asie",   hour: 0  },
    { name: "Europe", hour: 8  },
    { name: "USA",    hour: 16 },
  ];

  return (
    <div>
      <p className="font-mono text-4xl font-bold text-emerald-400 tabular-nums">{remain}</p>
      <div className="flex gap-3 mt-3">
        {sessions.map(s => {
          const now = new Date();
          const h = now.getUTCHours();
          const active = (s.hour <= h && h < s.hour + 8) || (s.hour === 0 && h >= 16);
          return (
            <span key={s.name} className={`px-2 py-0.5 rounded text-xs border ${
              active ? "border-emerald-700 text-emerald-400 bg-emerald-950" : "border-gray-800 text-gray-600"
            }`}>{s.name}</span>
          );
        })}
      </div>
    </div>
  );
}
