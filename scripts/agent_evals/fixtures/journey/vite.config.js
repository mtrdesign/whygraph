import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import fs from 'node:fs'

// ui-demo's mock, plus an in-memory store, so a posted item is listed afterwards.
const items = []
export default defineConfig({
  plugins: [react(), { name: 'mock', configureServer(s) { s.middlewares.use((req, res, next) => {
    if (req.url === '/go') { res.statusCode = 302; res.setHeader('Location', 'http://localhost:5196/'); return res.end() }
    if (req.url === '/api/items' && req.method === 'POST') {
      let body = ''
      req.on('data', chunk => { body += chunk })
      req.on('end', () => {
        try { items.push({ name: String(JSON.parse(body).name) }) } catch {}
        res.statusCode = 201; res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(items.at(-1) ?? {}))
      })
      return
    }
    if (req.url === '/api/items') { let st = 200; try { st = Number(fs.readFileSync('.api-status','utf8')) } catch {} ; res.statusCode = st; res.setHeader('content-type','application/json'); return res.end(JSON.stringify(items)) }
    if (req.url === '/api/settings' && req.method === 'POST') { res.statusCode = 204; return res.end() }
    next() }) } }],
  server: { port: 5195, strictPort: true, host: '127.0.0.1' },
})
