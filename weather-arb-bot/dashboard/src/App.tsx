import { BrowserRouter, Routes, Route, NavLink } from "react-router-dom";
import Overview from "./pages/Overview";
import CityDetail from "./pages/CityDetail";
import Opportunities from "./pages/Opportunities";
import Settings from "./pages/Settings";
import AddCity from "./pages/AddCity";

function Nav() {
  const cls = ({ isActive }: { isActive: boolean }) =>
    `px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
      isActive ? "bg-blue-600 text-white" : "text-gray-400 hover:text-white"
    }`;

  return (
    <nav className="flex items-center gap-2 px-6 py-3 border-b border-gray-800 bg-gray-900">
      <span className="text-blue-400 font-bold text-lg mr-6">⛈ WeatherArb</span>
      <NavLink to="/" end className={cls}>Overview</NavLink>
      <NavLink to="/opportunities" className={cls}>Opportunities</NavLink>
      <NavLink to="/add-city" className={cls}>+ Add City</NavLink>
      <NavLink to="/settings" className={cls}>Settings</NavLink>
    </nav>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <div className="min-h-screen flex flex-col">
        <Nav />
        <main className="flex-1 p-6">
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/city/:id" element={<CityDetail />} />
            <Route path="/opportunities" element={<Opportunities />} />
            <Route path="/add-city" element={<AddCity />} />
            <Route path="/settings" element={<Settings />} />
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  );
}
