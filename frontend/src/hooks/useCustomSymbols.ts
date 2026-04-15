import { useState, useEffect, useCallback } from 'react'

const STORAGE_KEY = 'custom_symbols_v1'

export interface CustomSymbol {
  code: string
  name: string
  addedAt: string
}

export function useCustomSymbols() {
  const [customSymbols, setCustomSymbols] = useState<CustomSymbol[]>([])
  const [isLoaded, setIsLoaded] = useState(false)

  // 从 localStorage 加载
  useEffect(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY)
      if (stored) {
        const parsed = JSON.parse(stored)
        if (Array.isArray(parsed)) {
          setCustomSymbols(parsed)
        }
      }
    } catch (e) {
      console.error('Failed to load custom symbols:', e)
    }
    setIsLoaded(true)
  }, [])

  // 保存到 localStorage
  useEffect(() => {
    if (isLoaded) {
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(customSymbols))
      } catch (e) {
        console.error('Failed to save custom symbols:', e)
      }
    }
  }, [customSymbols, isLoaded])

  const addSymbol = useCallback((code: string, name: string) => {
    const normalizedCode = code.trim()
    if (!normalizedCode) return false

    // 检查是否已存在
    if (customSymbols.some(s => s.code === normalizedCode)) {
      return false
    }

    const newSymbol: CustomSymbol = {
      code: normalizedCode,
      name: name.trim() || normalizedCode,
      addedAt: new Date().toISOString(),
    }

    setCustomSymbols(prev => [...prev, newSymbol])
    return true
  }, [customSymbols])

  const removeSymbol = useCallback((code: string) => {
    setCustomSymbols(prev => prev.filter(s => s.code !== code))
  }, [])

  const hasSymbol = useCallback((code: string) => {
    return customSymbols.some(s => s.code === code)
  }, [customSymbols])

  return {
    customSymbols,
    isLoaded,
    addSymbol,
    removeSymbol,
    hasSymbol,
  }
}
