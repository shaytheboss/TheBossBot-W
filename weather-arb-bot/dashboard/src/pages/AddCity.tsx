import { useState } from "react";
import { useNavigate } from "react-router-dom";
import AddCityModal from "../components/AddCityModal";

export default function AddCity() {
  const nav = useNavigate();
  const [done, setDone] = useState(false);

  if (done) {
    return (
      <div className="text-center py-20">
        <p className="text-green-400 text-lg font-medium mb-4">City added successfully!</p>
        <button
          onClick={() => nav("/")}
          className="bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg text-sm"
        >
          Back to Overview
        </button>
      </div>
    );
  }

  return (
    <AddCityModal
      onClose={() => nav("/")}
      onCreated={() => setDone(true)}
    />
  );
}
