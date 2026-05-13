import type { Signals } from "../api/client";

interface Props {
  signals: Signals;
}

function Row({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="flex justify-between items-start py-2 border-b border-gray-800 last:border-0">
      <span className="text-gray-400 text-sm">{label}</span>
      <div className="text-right">
        <span className="text-white text-sm font-medium">{value}</span>
        {sub && <p className="text-gray-500 text-xs">{sub}</p>}
      </div>
    </div>
  );
}

export default function SignalPanel({ signals }: Props) {
  const pm = signals.primary_metar as Record<string, number | null> | null;
  const rm = signals.reference_metar as Record<string, number | null> | null;
  const trend = signals.metar_trend;
  const wg = signals.wunderground_forecast;
  const gfs = signals.gfs_forecast;
  const ecmwf = signals.ecmwf_forecast;
  const mp = signals.market_price;

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
      <h3 className="text-white font-semibold mb-4">Signal Panel</h3>

      {pm && (
        <Row
          label="Primary METAR"
          value={`${pm.temperature_f ?? "—"}°F`}
          sub={`DP ${pm.dew_point_f ?? "—"}°F | Wind ${pm.wind_direction ?? "—"}°/${pm.wind_speed_kt ?? "—"}kt`}
        />
      )}

      {rm && (
        <Row
          label="Reference Station"
          value={`${rm.temperature_f ?? "—"}°F`}
          sub={`Wind ${rm.wind_direction ?? "—"}°/${rm.wind_speed_kt ?? "—"}kt`}
        />
      )}

      {trend && (
        <Row
          label="Temp Trend"
          value={`${trend.temp_rate_per_hour > 0 ? "+" : ""}${trend.temp_rate_per_hour}°F/hr`}
          sub={`Current: ${trend.current_temp_f}°F`}
        />
      )}

      {wg?.predicted_high_f != null && (
        <Row label="Wunderground Forecast" value={`${wg.predicted_high_f}°F`} />
      )}

      {gfs?.predicted_high_f != null && (
        <Row label="GFS Model" value={`${gfs.predicted_high_f}°F`} />
      )}

      {ecmwf?.predicted_high_f != null && (
        <Row label="ECMWF Model" value={`${ecmwf.predicted_high_f}°F`} />
      )}

      {mp && (
        <Row
          label="Market Price (YES)"
          value={`${Math.round(mp.yes_price * 100)}¢`}
          sub={`NO: ${Math.round(mp.no_price * 100)}¢`}
        />
      )}
    </div>
  );
}
