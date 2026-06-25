import { useEffect, useState } from "react"
import { useInterval } from "./useInterval"

export function useData<T>(fetcher: () => Promise<T>, refresh = 30000) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const load = async () => {
    try {
      setError(null)
      const res = await fetcher()
      setData(res)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [fetcher])

  useInterval(load, refresh)

  return { data, error, loading, reload: load }
}
