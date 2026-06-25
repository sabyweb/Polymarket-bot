import { useEffect, useRef } from "react"

export function useInterval(callback: () => void, delay: number | null) {
  const savedRef = useRef(callback)
  useEffect(() => {
    savedRef.current = callback
  }, [callback])
  useEffect(() => {
    if (delay === null) return
    const id = setInterval(() => savedRef.current(), delay)
    return () => clearInterval(id)
  }, [delay])
}
