import { useNavigate } from "react-router-dom";
import type { City, CityCurrentData } from "../api/client";

interface Props {
  city: City;
  current: CityCurrentData | null;
}

export default function CityCard({ city, current }: Props) {
  const nav = useNavigate();
  const metar = current?.latest_metar;
  const forecast = current?.latest_forecast;

  const trend = metar?.temperature_f
    ? metar.temperature_f > (forecast?.predicted_high_f ?? 0) * 0.95
      ? "↑"
      : "→"
    : "—";

  return (
    <div
      onClick={() => nav(`/city/${city.id}`)}
      className="bg-gray-900 border border-gray-800 rounded-xl p-5 cursor-pointer
                 hover:border-blue-700 transition-colors"
    >
      <div className="flex justify-between items-start mb-3">
        <div>
          <h2 className="text-white font-semibold text-lg">{city.name}</h2>
          <span className="text-gray-500 text-xs">{city.primary_icao}</span>
        </div>
        {city.active ? (
          <span className="text-green-400 text-xs bg-green-900/30 px-2 py-1 rounded-full">
            LIVE
          </span>
        ) : (
          <span className="text-gray-500 text-xs bg-gray-800 px-2 py-1 rounded-full">
            PAUSED
          </span>
        )}
      </div>

      <div className="grid grid-cols-3 gap-3 text-center">
        <div>
          <p className="text-2xl font-bold text-white">
            {metar?.temperature_f != null ? `${metar.temperature_f}°` : "—"}
          </p>
          <p className="text-gray-500 text-xs">Current</p>
        </div>
        <div>
          <p className="text-2xl font-bold text-blue-400">
            {forecast?.predicted_high_f != null ? `${forecast.predicted_high_f}°` : "—"}
          </p>
          <p className="text-gray-500 text-xs">Forecast High</p>
        </div>
        <div>
          <p className="text-2xl font-bold text-yellow-400">{trend}</p>
          <p className="text-gray-500 text-xs">Trend</p>
        </div>
      </div>

      {forecast?.conditions && (
        <p className="mt-3 text-gray-400 text-xs truncate">{forecast.conditions}</p>
      )}
    </div>
  );
}
