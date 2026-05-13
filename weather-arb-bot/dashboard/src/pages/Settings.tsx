import { useEffect, useState } from "react";
import { api } from "../api/client";

export default function Settings() {
  const [health, setHealth] = useState<{ status: string; db: string } | null>(null);

  useEffect(() => {
    api.health().then((h) => setHealth(h as { status: string; db: string })).catch(() => {});
  }, []);

  return (
    <div className="max-w-2xl">
      <h1 className="text-2xl font-bold text-white mb-6">Settings</h1>

      <section className="bg-gray-900 border border-gray-800 rounded-xl p-5 mb-6">
        <h2 className="text-white font-semibold mb-4">System Health</h2>
        {health ? (
          <div className="space-y-2 text-sm">
            <Row label="API status" value={health.status} ok={health.status === "ok"} />
            <Row label="Database" value={health.db} ok={health.db === "connected"} />
          </div>
        ) : (
          <p className="text-gray-500 text-sm">Checking…</p>
        )}
      </section>

      <section className="bg-gray-900 border border-gray-800 rounded-xl p-5 mb-6">
        <h2 className="text-white font-semibold mb-4">Telegram Bot</h2>
        <p className="text-gray-400 text-sm mb-3">
          Send <code className="bg-gray-800 px-1 rounded">/start</code> to your bot to register.
          Then use:
        </p>
        <ul className="text-gray-400 text-sm space-y-1">
          <li><code className="text-blue-400">/watch SF</code> — watch San Francisco</li>
          <li><code className="text-blue-400">/unwatch SF</code> — stop watching</li>
          <li><code className="text-blue-400">/status</code> — current market status</li>
          <li><code className="text-blue-400">/settings</code> — configure alert thresholds</li>
        </ul>
      </section>

      <section className="bg-gray-900 border border-gray-800 rounded-xl p-5">
        <h2 className="text-white font-semibold mb-4">Environment Reference</h2>
        <p className="text-gray-400 text-sm mb-3">
          Configure these variables on Railway / in your <code className="bg-gray-800 px-1 rounded">.env</code>:
        </p>
        <div className="space-y-1 text-xs font-mono">
          {[
            "DATABASE_URL",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_WEBHOOK_SECRET",
            "METAR_FETCH_INTERVAL (default 300s)",
            "POLYMARKET_FETCH_INTERVAL (default 30s)",
            "ANALYZER_RUN_INTERVAL (default 120s)",
            "MIN_CONFIDENCE_FOR_ALERT (default 60)",
            "MIN_EDGE_FOR_ALERT (default 0.15)",
          ].map((v) => (
            <p key={v} className="text-gray-500">
              <span className="text-blue-400">{v.split(" ")[0]}</span>
              {v.includes(" ") && (
                <span className="text-gray-600"> {v.slice(v.indexOf(" "))}</span>
              )}
            </p>
          ))}
        </div>
      </section>
    </div>
  );
}

function Row({ label, value, ok }: { label: string; value: string; ok: boolean }) {
  return (
    <div className="flex justify-between">
      <span className="text-gray-400">{label}</span>
      <span className={ok ? "text-green-400" : "text-red-400"}>{value}</span>
    </div>
  );
}
