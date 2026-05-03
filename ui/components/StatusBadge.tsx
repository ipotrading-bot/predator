export function StatusBadge({ scanning }: { scanning: boolean }) {
  return (
    <div className={`flex items-center gap-2 px-4 py-2 rounded-full border text-sm font-medium ${
      scanning
        ? "bg-amber-950 border-amber-700 text-amber-300"
        : "bg-emerald-950 border-emerald-700 text-emerald-300"
    }`}>
      <span className={`w-2 h-2 rounded-full ${scanning ? "bg-amber-400 animate-ping" : "bg-emerald-400"}`} />
      {scanning ? "Scanning..." : "Online"}
    </div>
  );
}
