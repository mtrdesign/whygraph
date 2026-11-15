import { createRoot } from 'react-dom/client'
import { routes, useRoute } from './routes.jsx'

function App() {
  const Page = routes[useRoute()]
  return Page ? <Page /> : <main><h1>Not found</h1></main>
}
createRoot(document.getElementById('root')).render(<App />)
