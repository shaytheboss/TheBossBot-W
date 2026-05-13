import { useEffect, useState } from "react";
import { api, type City, type CityCurrentData } from "../api/client";
import CityCard from "../components/CityCard";
import AlertBar from "../components/AlertBar";

export default function Overview() {
  const [cities, setCities] = useState<City[]>([]);
  const [currentData, setCurrentData] = useState<Record<number, CityCurrentData>>({});
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api.cities.list().then((cs) => {
      setCities(cs);
      setLoading(false);
      cs.forEach((c) => {
        api.cities.current(c.id).then((d) => {
          setCurrentData((prev) => ({ ...prev, [c.id]: d }));
        }).catch(() => {});
      });
    }).catch(() => setLoading(false));
  }, []);

  return (
    <div>
      <div className="flex justify-between items-center mb-6">
        <h1 className="text-2xl font-bold text-white">Market Overview</h1>
        <span className="text-gray-500 text-sm">
          {cities.length} {cities.length === 1 ? "city" : "cities"} monitored
        </span>
      </div>

      <AlertBar />

      {loading ? (
        <div className="text-gray-500 text-center py-20">Loading…</div>
      ) : cities.length === 0 ? (
        <div className="text-center py-20">
          <p className="text-gray-500 mb-4">No cities configured yet.</p>
          <a
            href="/add-city"
            className="bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg text-sm"
          >
            + Add your first city
          </a>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {cities.map((city) => (
            <CityCard key={city.id} city={city} current={currentData[city.id] ?? null} />
          ))}
        </div>
      )}
    </div>
  );
}
