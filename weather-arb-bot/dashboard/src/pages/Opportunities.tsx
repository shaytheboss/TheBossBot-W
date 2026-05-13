import { useEffect, useState } from "react";
import { api, type Opportunity } from "../api/client";
import { formatDistanceToNow, format } from "date-fns";

function Badge({ children, color }: { children: React.ReactNode; color: string }) {
  return (
    <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${color}`}>
      {children}
    </span>
  );
}

function OppRow({ o }: { o: Opportunity }) {
  const edge = Math.round(o.edge * 100);
  const conf = o.confidence_score;
  const isActive = !o.closed_at;

  return (
    <tr className="border-b border-gray-800/50 hover:bg-gray-800/30">
      <td className="py-3 pr-4">
        <span className="text-white font-medium">#{o.id}</span>
        <p className="text-gray-500 text-xs">
          {formatDistanceToNow(new Date(o.detected_at), { addSuffix: true })}
        </p>
      </td>
      <td className="py-3 pr-4">
        <Badge color={o.side === "YES" ? "bg-blue-900 text-blue-300" : "bg-purple-900 text-purple-300"}>
          {o.side}
        </Badge>
      </td>
      <td className="py-3 pr-4 text-right text-gray-400">
        {Math.round(o.market_price * 100)}¢
      </td>
      <td className="py-3 pr-4 text-right text-blue-400">
        {Math.round(o.estimated_true_prob * 100)}%
      </td>
      <td className={`py-3 pr-4 text-right font-medium ${edge >= 20 ? "text-green-400" : "text-yellow-400"}`}>
        +{edge}pp
      </td>
      <td className="py-3 pr-4 text-right">
        <span className={`text-sm ${conf >= 70 ? "text-green-400" : conf >= 60 ? "text-yellow-400" : "text-gray-400"}`}>
          {conf}
        </span>
      </td>
      <td className="py-3">
        {isActive ? (
          <Badge color="bg-green-900 text-green-300">OPEN</Badge>
        ) : (
          <Badge color="bg-gray-800 text-gray-400">CLOSED</Badge>
        )}
      </td>
    </tr>
  );
}

export default function Opportunities() {
  const [active, setActive] = useState<Opportunity[]>([]);
  const [history, setHistory] = useState<Opportunity[]>([]);
  const [tab, setTab] = useState<"active" | "history">("active");

  useEffect(() => {
    api.opportunities.active().then(setActive).catch(() => {});
    api.opportunities.history(100).then(setHistory).catch(() => {});
  }, []);

  const rows = tab === "active" ? active : history;

  return (
    <div>
      <h1 className="text-2xl font-bold text-white mb-6">Opportunities</h1>

      <div className="flex gap-2 mb-6">
        <button
          onClick={() => setTab("active")}
          className={`px-4 py-2 rounded-lg text-sm font-medium ${
            tab === "active" ? "bg-blue-600 text-white" : "bg-gray-800 text-gray-400"
          }`}
        >
          Active ({active.length})
        </button>
        <button
          onClick={() => setTab("history")}
          className={`px-4 py-2 rounded-lg text-sm font-medium ${
            tab === "history" ? "bg-blue-600 text-white" : "bg-gray-800 text-gray-400"
          }`}
        >
          History ({history.length})
        </button>
      </div>

      {rows.length === 0 ? (
        <div className="text-center py-20 text-gray-500">
          {tab === "active" ? "No active opportunities right now." : "No historical opportunities yet."}
        </div>
      ) : (
        <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-gray-500 text-xs border-b border-gray-800 bg-gray-900/50">
                  <th className="text-left px-4 py-3">ID / Time</th>
                  <th className="text-left px-4 py-3">Side</th>
                  <th className="text-right px-4 py-3">Market</th>
                  <th className="text-right px-4 py-3">Est. Prob</th>
                  <th className="text-right px-4 py-3">Edge</th>
                  <th className="text-right px-4 py-3">Conf</th>
                  <th className="px-4 py-3">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-800/50">
                {rows.map((o) => (
                  <OppRow key={o.id} o={o} />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
