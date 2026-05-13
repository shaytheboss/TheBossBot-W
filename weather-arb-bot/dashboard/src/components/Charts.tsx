import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
  Legend,
} from "recharts";
import { format } from "date-fns";
import type { MetarRow, PriceRow } from "../api/client";

interface TempChartProps {
  data: MetarRow[];
}

export function TempChart({ data }: TempChartProps) {
  const chartData = data.map((r) => ({
    time: format(new Date(r.observed_at), "HH:mm"),
    temp: r.temperature_f,
    dew: r.dew_point_f,
  }));

  return (
    <ResponsiveContainer width="100%" height={220}>
      <LineChart data={chartData}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis dataKey="time" stroke="#6b7280" tick={{ fontSize: 11 }} />
        <YAxis stroke="#6b7280" tick={{ fontSize: 11 }} unit="°F" />
        <Tooltip
          contentStyle={{ background: "#111827", border: "1px solid #374151" }}
          labelStyle={{ color: "#9ca3af" }}
        />
        <Legend />
        <Line
          type="monotone"
          dataKey="temp"
          name="Temp °F"
          stroke="#60a5fa"
          dot={false}
          strokeWidth={2}
        />
        <Line
          type="monotone"
          dataKey="dew"
          name="Dew Point °F"
          stroke="#34d399"
          dot={false}
          strokeWidth={2}
          strokeDasharray="4 2"
        />
      </LineChart>
    </ResponsiveContainer>
  );
}

interface PriceChartProps {
  data: Record<string, PriceRow[]>;
}

const BUCKET_COLORS: Record<string, string> = {
  "64-65": "#f59e0b",
  "66+": "#ef4444",
  "62-63": "#60a5fa",
  "60-61": "#34d399",
};

export function PriceChart({ data }: PriceChartProps) {
  const merged: Record<string, Record<string, number>> = {};
  Object.entries(data).forEach(([bucket, rows]) => {
    rows.forEach((r) => {
      const t = format(new Date(r.timestamp), "HH:mm");
      merged[t] = merged[t] ?? {};
      merged[t][bucket] = r.yes_price;
    });
  });

  const chartData = Object.entries(merged)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([time, vals]) => ({ time, ...vals }));

  const buckets = Object.keys(data);

  return (
    <ResponsiveContainer width="100%" height={220}>
      <LineChart data={chartData}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1f2937" />
        <XAxis dataKey="time" stroke="#6b7280" tick={{ fontSize: 11 }} />
        <YAxis
          stroke="#6b7280"
          tick={{ fontSize: 11 }}
          tickFormatter={(v) => `${Math.round(v * 100)}¢`}
          domain={[0, 1]}
        />
        <Tooltip
          contentStyle={{ background: "#111827", border: "1px solid #374151" }}
          formatter={(v: number) => `${Math.round(v * 100)}¢`}
        />
        <Legend />
        {buckets.map((b) => (
          <Line
            key={b}
            type="monotone"
            dataKey={b}
            name={b}
            stroke={BUCKET_COLORS[b] ?? "#a78bfa"}
            dot={false}
            strokeWidth={2}
          />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}
