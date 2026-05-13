const BASE = import.meta.env.VITE_API_URL ?? "/api";

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) throw new Error(`API ${res.status}: ${path}`);
  return res.json();
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`API ${res.status}: ${path}`);
  return res.json();
}

export const api = {
  cities: {
    list: () => get<City[]>("/cities"),
    current: (id: number) => get<CityCurrentData>(`/cities/${id}/current`),
    history: (id: number, from?: string, to?: string) => {
      const params = new URLSearchParams();
      if (from) params.set("from_dt", from);
      if (to) params.set("to_dt", to);
      return get<MetarRow[]>(`/cities/${id}/history?${params}`);
    },
    signals: (id: number) => get<Signals>(`/cities/${id}/signals`),
    create: (body: CityCreatePayload) => post<City>("/cities", body),
  },
  markets: {
    list: () => get<Market[]>("/markets"),
    analysis: (id: number) => get<BucketAnalysis[]>(`/markets/${id}/analysis`),
    prices: (id: number, hours?: number) =>
      get<Record<string, PriceRow[]>>(`/markets/${id}/prices?hours=${hours ?? 24}`),
  },
  opportunities: {
    active: () => get<Opportunity[]>("/opportunities"),
    history: (limit?: number) =>
      get<Opportunity[]>(`/opportunities/history?limit=${limit ?? 50}`),
  },
  health: () => get<{ status: string }>("/health"),
};

// ── Types ───────────────────────────────────────────────────────────────────────────

export interface City {
  id: number;
  name: string;
  primary_icao: string;
  reference_icao: string | null;
  wunderground_url: string;
  timezone: string;
  active: boolean;
}

export interface CityCreatePayload {
  name: string;
  primary_icao: string;
  reference_icao?: string;
  wunderground_url: string;
  nws_lat?: number;
  nws_lon?: number;
  timezone: string;
  buoy_id?: string;
}

export interface MetarRow {
  id: number;
  icao: string;
  observed_at: string;
  temperature_f: number | null;
  dew_point_f: number | null;
  wind_direction: number | null;
  wind_speed_kt: number | null;
  conditions: string | null;
}

export interface CityCurrentData {
  city: City;
  latest_metar: MetarRow | null;
  latest_forecast: { predicted_high_f: number | null; conditions: string | null } | null;
}

export interface Signals {
  primary_metar: Record<string, unknown> | null;
  reference_metar: Record<string, unknown> | null;
  metar_trend: { temp_rate_per_hour: number; current_temp_f: number } | null;
  wunderground_forecast: { predicted_high_f: number | null } | null;
  gfs_forecast: { predicted_high_f: number | null } | null;
  ecmwf_forecast: { predicted_high_f: number | null } | null;
  market_price: { yes_price: number; no_price: number } | null;
}

export interface Market {
  id: number;
  city_id: number;
  question: string;
  event_date: string;
  resolved: boolean;
}

export interface BucketAnalysis {
  bucket: string;
  market_price: number | null;
  true_prob: number;
  confidence: number;
  edge: number | null;
}

export interface PriceRow {
  timestamp: string;
  yes_price: number;
  no_price: number;
}

export interface Opportunity {
  id: number;
  outcome_id: number;
  detected_at: string;
  side: "YES" | "NO";
  market_price: number;
  estimated_true_prob: number;
  edge: number;
  confidence_score: number;
  closed_at: string | null;
}
