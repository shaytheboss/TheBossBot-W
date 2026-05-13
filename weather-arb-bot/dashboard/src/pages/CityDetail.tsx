import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api, type MetarRow, type Signals, type BucketAnalysis } from "../api/client";
import { TempChart, PriceChart } from "../components/Charts";
import SignalPanel from "../components/SignalPanel";

export default function CityDetail() {
  const { id } = useParams<{ id: string }>();
  const cityId = Number(id);

  const [history, setHistory] = useState<MetarRow[]>([]);
  const [signals, setSignals] = useState<Signals | null>(null);
  const [analysis, setAnalysis] = useState<BucketAnalysis[]>([]);
  const [prices, setPrices] = useState<Record<string, { timestamp: string; yes_price: number; no_price: number }[]>>({});
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!cityId) return;
    Promise.all([
      api.cities.history(cityId),
      api.cities.signals(cityId).catch(() => null),
    ]).then(([h, s]) => {
      setHistory(h);
      setSignals(s);
      setLoading(false);
    }).catch(() => setLoading(false));
  }, [cityId]);

  if (loading) return <div className="text-gray-500 text-center py-20">Loading…</div>;

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold text-white">City Detail #{cityId}</h1>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
          <h3 className="text-white font-semibold mb-4">Temperature & Dew Point (24h)</h3>
          {history.length > 0 ? (
            <TempChart data={history} />
          ) : (
            <p className="text-gray-500 text-sm">No METAR data yet.</p>
          )}
        </div>

        <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
          <h3 className="text-white font-semibold mb-4">Market Prices (24h)</h3>
          {Object.keys(prices).length > 0 ? (
            <PriceChart data={prices} />
          ) : (
            <p className="text-gray-500 text-sm">No market price data yet.</p>
          )}
        </div>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        {signals && <SignalPanel signals={signals} />}

        {analysis.length > 0 && (
          <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
            <h3 className="text-white font-semibold mb-4">Analysis per Bucket</h3>
            <table className="w-full text-sm">
              <thead>
                <tr className="text-gray-500 text-xs border-b border-gray-800">
                  <th className="text-left pb-2">Bucket</th>
                  <th className="text-right pb-2">Market</th>
                  <th className="text-right pb-2">Est. Prob</th>
                  <th className="text-right pb-2">Edge</th>
                  <th className="text-right pb-2">Conf</th>
                </tr>
              </thead>
              <tbody>
                {analysis.map((row) => (
                  <tr key={row.bucket} className="border-b border-gray-800/50">
                    <td className="py-2 text-white font-medium">{row.bucket}</td>
                    <td className="py-2 text-right text-gray-400">
                      {row.market_price != null ? `${Math.round(row.market_price * 100)}¢` : "—"}
                    </td>
                    <td className="py-2 text-right text-blue-400">
                      {Math.round(row.true_prob * 100)}%
                    </td>
                    <td
                      className={`py-2 text-right font-medium ${
                        (row.edge ?? 0) > 0.10 ? "text-green-400" : "text-gray-400"
                      }`}
                    >
                      {row.edge != null
                        ? `${row.edge > 0 ? "+" : ""}${Math.round(row.edge * 100)}pp`
                        : "—"}
                    </td>
                    <td className="py-2 text-right text-gray-400">{row.confidence}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
        <h3 className="text-white font-semibold mb-4">METAR Log</h3>
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-gray-500 text-xs border-b border-gray-800">
                <th className="text-left pb-2">Time (UTC)</th>
                <th className="text-right pb-2">Temp °F</th>
                <th className="text-right pb-2">Dew °F</th>
                <th className="text-right pb-2">Wind dir</th>
                <th className="text-right pb-2">Wind kt</th>
                <th className="text-left pb-2 pl-4">Conditions</th>
              </tr>
            </thead>
            <tbody>
              {history.slice(-24).reverse().map((r) => (
                <tr key={r.id} className="border-b border-gray-800/50">
                  <td className="py-1 text-gray-400">
                    {new Date(r.observed_at).toUTCString().slice(17, 22)}Z
                  </td>
                  <td className="py-1 text-right text-white">{r.temperature_f ?? "—"}</td>
                  <td className="py-1 text-right text-gray-400">{r.dew_point_f ?? "—"}</td>
                  <td className="py-1 text-right text-gray-400">{r.wind_direction ?? "—"}°</td>
                  <td className="py-1 text-right text-gray-400">{r.wind_speed_kt ?? "—"}</td>
                  <td className="py-1 pl-4 text-gray-400 text-xs">{r.conditions ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
