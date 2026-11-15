import { useEffect, useState } from 'react'
import { navigate } from './routes.jsx'

export function Items() {
  const [items, setItems] = useState(null)
  useEffect(() => { fetch('/api/items').then(r => r.json()).then(setItems).catch(() => setItems([])) }, [])
  return <main><h1>Items</h1><p>{items === null ? 'Loading' : `${items.length} items`}</p>
    <button>Add item</button>
    <a href="/settings" onClick={e => { e.preventDefault(); navigate('/settings') }}>Settings</a>
    <ul>{(items ?? []).map((item, i) => <li key={i}>{item.name}</li>)}</ul>
  </main>
}
