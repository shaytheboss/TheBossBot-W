import { useState } from "react";
import { api, type CityCreatePayload } from "../api/client";

interface Props {
  onClose: () => void;
  onCreated: () => void;
}

export default function AddCityModal({ onClose, onCreated }: Props) {
  const [form, setForm] = useState<CityCreatePayload>({
    name: "",
    primary_icao: "",
    reference_icao: "",
    wunderground_url: "",
    nws_lat: undefined,
    nws_lon: undefined,
    timezone: "America/Los_Angeles",
    buoy_id: "",
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const set = (k: keyof CityCreatePayload, v: string | number | undefined) =>
    setForm((f) => ({ ...f, [k]: v }));

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError("");
    try {
      const payload = {
        ...form,
        reference_icao: form.reference_icao || undefined,
        buoy_id: form.buoy_id || undefined,
      };
      await api.cities.create(payload);
      onCreated();
      onClose();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to create city");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50">
      <div className="bg-gray-900 border border-gray-700 rounded-xl p-6 w-full max-w-lg">
        <h2 className="text-white font-bold text-xl mb-5">Add New City</h2>
        <form onSubmit={submit} className="space-y-4">
          <Field label="City Name" required>
            <input
              className={input}
              value={form.name}
              onChange={(e) => set("name", e.target.value)}
              placeholder="San Francisco"
              required
            />
          </Field>
          <div className="grid grid-cols-2 gap-4">
            <Field label="Primary ICAO" required>
              <input
                className={input}
                value={form.primary_icao}
                onChange={(e) => set("primary_icao", e.target.value.toUpperCase())}
                placeholder="KSFO"
                maxLength={4}
                required
              />
            </Field>
            <Field label="Reference ICAO">
              <input
                className={input}
                value={form.reference_icao}
                onChange={(e) => set("reference_icao", e.target.value.toUpperCase())}
                placeholder="KHAF"
                maxLength={4}
              />
            </Field>
          </div>
          <Field label="Wunderground URL" required>
            <input
              className={input}
              value={form.wunderground_url}
              onChange={(e) => set("wunderground_url", e.target.value)}
              placeholder="https://www.wunderground.com/weather/us/ca/san-francisco"
              required
            />
          </Field>
          <div className="grid grid-cols-2 gap-4">
            <Field label="NWS Latitude">
              <input
                className={input}
                type="number"
                step="0.0001"
                value={form.nws_lat ?? ""}
                onChange={(e) => set("nws_lat", e.target.value ? parseFloat(e.target.value) : undefined)}
                placeholder="37.6213"
              />
            </Field>
            <Field label="NWS Longitude">
              <input
                className={input}
                type="number"
                step="0.0001"
                value={form.nws_lon ?? ""}
                onChange={(e) => set("nws_lon", e.target.value ? parseFloat(e.target.value) : undefined)}
                placeholder="-122.379"
              />
            </Field>
          </div>
          <div className="grid grid-cols-2 gap-4">
            <Field label="Timezone">
              <select
                className={input}
                value={form.timezone}
                onChange={(e) => set("timezone", e.target.value)}
              >
                <option value="America/Los_Angeles">America/Los_Angeles</option>
                <option value="America/New_York">America/New_York</option>
                <option value="America/Chicago">America/Chicago</option>
                <option value="America/Denver">America/Denver</option>
                <option value="America/Phoenix">America/Phoenix</option>
              </select>
            </Field>
            <Field label="Nearest NDBC Buoy ID">
              <input
                className={input}
                value={form.buoy_id}
                onChange={(e) => set("buoy_id", e.target.value)}
                placeholder="46026"
              />
            </Field>
          </div>
          {error && <p className="text-red-400 text-sm">{error}</p>}
          <div className="flex gap-3 pt-2">
            <button
              type="submit"
              disabled={loading}
              className="flex-1 bg-blue-600 hover:bg-blue-700 text-white font-medium py-2 rounded-lg transition-colors disabled:opacity-50"
            >
              {loading ? "Creating…" : "Add City"}
            </button>
            <button
              type="button"
              onClick={onClose}
              className="px-4 bg-gray-800 hover:bg-gray-700 text-gray-300 rounded-lg transition-colors"
            >
              Cancel
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

const input =
  "w-full bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-white text-sm focus:outline-none focus:border-blue-500";

function Field({ label, children, required }: { label: string; children: React.ReactNode; required?: boolean }) {
  return (
    <div>
      <label className="block text-gray-400 text-xs mb-1">
        {label} {required && <span className="text-red-400">*</span>}
      </label>
      {children}
    </div>
  );
}
