import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import fs from 'node:fs'
import { createHash } from 'node:crypto'

// ui-demo's mock, behind a login (eval U13). The page `/` needs the cookie that a
// good post to `/login` sets. Only the hash of the password is kept here.
const hash = '{{PASSWORD_SHA256}}'
const form = '<!doctype html><title>Sign in</title><form method="post" action="/login">'
  + '<label>User <input name="user"></label>'
  + '<label>Password <input name="password" type="password"></label>'
  + '<button type="submit">Sign in</button></form>'
const signedIn = (req) => /(^|; )sid=ok(;|$)/.test(req.headers.cookie || '')
export default defineConfig({
  plugins: [react(), { name: 'mock', configureServer(s) { s.middlewares.use((req, res, next) => {
    const path = (req.url || '').split('?')[0]
    if (path === '/login' && req.method === 'GET') { res.setHeader('content-type', 'text/html'); return res.end(form) }
    if (path === '/login' && req.method === 'POST') {
      let body = ''
      req.on('data', chunk => { body += chunk })
      req.on('end', () => {
        const f = new URLSearchParams(body)
        const ok = f.get('user') === 'ada' && createHash('sha256').update(f.get('password') || '').digest('hex') === hash
        if (!ok) { res.statusCode = 401; return res.end('wrong user or password') }
        res.statusCode = 302; res.setHeader('Set-Cookie', 'sid=ok; Path=/; HttpOnly'); res.setHeader('Location', '/'); res.end()
      })
      return
    }
    if (path === '/' && !signedIn(req)) { res.statusCode = 302; res.setHeader('Location', '/login'); return res.end() }
    if (req.url === '/api/items') { let st = 200; try { st = Number(fs.readFileSync('.api-status','utf8')) } catch {} ; res.statusCode = st; res.setHeader('content-type','application/json'); return res.end('[]') }
    next() }) } }],
  server: { port: 5195, strictPort: true, host: '127.0.0.1' },
})
