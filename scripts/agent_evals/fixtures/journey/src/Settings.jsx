import { useState } from 'react'
import { navigate } from './routes.jsx'

export function Settings() {
  const [name, setName] = useState('')
  const [saving, setSaving] = useState(false)
  async function save() {
    setSaving(true)
    await fetch('/api/settings', { method: 'POST', body: JSON.stringify({ name }) })
    navigate('/')
  }
  return <main><h1>Settings</h1>
    <label>Name <input value={name} onChange={e => setName(e.target.value)} /></label>
    <button disabled={saving} onClick={save}>{saving ? 'Saving' : 'Save'}</button>
  </main>
}
