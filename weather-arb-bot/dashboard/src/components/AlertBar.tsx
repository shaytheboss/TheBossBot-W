import { useEffect, useState } from "react";
import { api, type Opportunity } from "../api/client";
import { formatDistanceToNow } from "date-fns";

export default function AlertBar() {
  const [opps, setOpps] = useState<Opportunity[]>([]);

  useEffect(() => {
    api.opportunities.active().then(setOpps).catch(() => {});
    const t = setInterval(() => {
      api.opportunities.active().then(setOpps).catch(() => {});
    }, 30_000);
    return () => clearInterval(t);
  }, []);

  if (opps.length === 0) return null;

  return (
    <div className="mb-6 space-y-2">
      {opps.map((o) => (
        <div
          key={o.id}
          className="flex items-center gap-4 bg-yellow-900/30 border border-yellow-700
                     rounded-lg px-4 py-3 text-sm"
        >
          <span className="text-yellow-400 font-bold text-lg">🎯</span>
          <div className="flex-1">
            <span className="text-yellow-200 font-semibold">
              Opportunity #{o.id}
            </span>
            <span className="text-gray-400 ml-2">
              {o.side} @ {Math.round(o.market_price * 100)}¢ —
              est. {Math.round(o.estimated_true_prob * 100)}% — edge{" "}
              +{Math.round(o.edge * 100)}pp
            </span>
          </div>
          <span className="text-gray-500 text-xs whitespace-nowrap">
            {formatDistanceToNow(new Date(o.detected_at), { addSuffix: true })}
          </span>
          <span className="text-green-400 text-xs bg-green-900/30 px-2 py-0.5 rounded-full">
            conf {o.confidence_score}
          </span>
        </div>
      ))}
    </div>
  );
}
