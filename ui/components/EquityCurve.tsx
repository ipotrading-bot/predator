"use client";
import { useEffect, useRef } from "react";

interface Point { timestamp: string; balance: number; drawdown: number; }

export default function EquityCurve({ data }: { data: Point[] }) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas || !data.length) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const W = canvas.width  = canvas.offsetWidth  * window.devicePixelRatio;
    const H = canvas.height = canvas.offsetHeight * window.devicePixelRatio;
    ctx.scale(window.devicePixelRatio, window.devicePixelRatio);
    const w = canvas.offsetWidth;
    const h = canvas.offsetHeight;

    const balances = data.map((d) => d.balance);
    const min = Math.min(...balances) * 0.98;
    const max = Math.max(...balances) * 1.02;
    const PAD = { t: 16, r: 16, b: 24, l: 56 };

    ctx.clearRect(0, 0, w, h);

    // Grid
    ctx.strokeStyle = "rgba(255,255,255,0.04)";
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = PAD.t + (h - PAD.t - PAD.b) * (i / 4);
      ctx.beginPath(); ctx.moveTo(PAD.l, y); ctx.lineTo(w - PAD.r, y); ctx.stroke();
      const val = max - (max - min) * (i / 4);
      ctx.fillStyle = "rgba(255,255,255,0.3)";
      ctx.font = "10px monospace";
      ctx.fillText(`${Math.round(val).toLocaleString()}€`, 0, y + 4);
    }

    const px = (i: number) => PAD.l + ((w - PAD.l - PAD.r) * i) / (data.length - 1);
    const py = (v: number) => PAD.t + (h - PAD.t - PAD.b) * (1 - (v - min) / (max - min));

    // Baseline 10k
    const baseY = py(10_000);
    ctx.strokeStyle = "rgba(255,255,255,0.08)";
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(PAD.l, baseY); ctx.lineTo(w - PAD.r, baseY); ctx.stroke();
    ctx.setLineDash([]);

    // Gradient fill
    const grad = ctx.createLinearGradient(0, PAD.t, 0, h - PAD.b);
    grad.addColorStop(0, "rgba(16,185,129,0.2)");
    grad.addColorStop(1, "rgba(16,185,129,0)");

    ctx.beginPath();
    ctx.moveTo(px(0), h - PAD.b);
    data.forEach((d, i) => ctx.lineTo(px(i), py(d.balance)));
    ctx.lineTo(px(data.length - 1), h - PAD.b);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    // Line
    ctx.strokeStyle = "#10b981";
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.beginPath();
    data.forEach((d, i) => i === 0 ? ctx.moveTo(px(i), py(d.balance)) : ctx.lineTo(px(i), py(d.balance)));
    ctx.stroke();
  }, [data]);

  return data.length === 0
    ? <div className="h-48 flex items-center justify-center text-gray-600 text-sm">En attente de données...</div>
    : <canvas ref={ref} style={{ width: "100%", height: "240px", display: "block" }} />;
}
