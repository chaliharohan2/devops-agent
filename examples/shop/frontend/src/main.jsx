import { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";

const API = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

function App() {
  const [items, setItems] = useState([]);
  const [error, setError] = useState(null);
  const [name, setName] = useState("");
  const [price, setPrice] = useState("");

  const load = () =>
    fetch(`${API}/api/items`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(setItems)
      .catch((e) => setError(String(e)));

  useEffect(() => { load(); }, []);

  async function add(e) {
    e.preventDefault();
    await fetch(`${API}/api/items`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, price: Number(price) }),
    });
    setName(""); setPrice("");
    load();
  }

  return (
    <main style={{ fontFamily: "system-ui", maxWidth: 560, margin: "40px auto", padding: 16 }}>
      <h1>Shop</h1>
      <p style={{ color: "#666" }}>API: {API}</p>
      {error && <p style={{ color: "crimson" }}>Could not load items: {error}</p>}
      <ul data-testid="items">
        {items.map((i) => <li key={i.id}>{i.name} — ${i.price.toFixed(2)}</li>)}
      </ul>
      <form onSubmit={add} style={{ display: "flex", gap: 8 }}>
        <input placeholder="name" value={name} onChange={(e) => setName(e.target.value)} />
        <input placeholder="price" value={price} onChange={(e) => setPrice(e.target.value)} />
        <button>Add</button>
      </form>
    </main>
  );
}

createRoot(document.getElementById("root")).render(<StrictMode><App /></StrictMode>);
