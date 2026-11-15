import { useEffect, useState } from 'react'
import { Items } from './Items.jsx'
import { Settings } from './Settings.jsx'

export const routes = { '/': Items, '/settings': Settings }

export function navigate(path) {
  history.pushState({}, '', path)
  dispatchEvent(new PopStateEvent('popstate'))
}

export function useRoute() {
  const [path, setPath] = useState(location.pathname)
  useEffect(() => {
    const onPop = () => setPath(location.pathname)
    addEventListener('popstate', onPop)
    return () => removeEventListener('popstate', onPop)
  }, [])
  return path
}
