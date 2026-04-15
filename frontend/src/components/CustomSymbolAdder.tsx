import { useState, useCallback } from 'react'

interface CustomSymbolAdderProps {
  onAdd: (code: string, name: string) => boolean
  onRemove: (code: string) => void
  customSymbols: Array<{ code: string; name: string }>
}

async function fetchStockName(code: string): Promise<string | null> {
  try {
    const resp = await fetch(`/api/stock/name?code=${encodeURIComponent(code)}`)
    if (!resp.ok) return null
    const data = await resp.json()
    return data.name || null
  } catch {
    return null
  }
}

export function CustomSymbolAdder({ onAdd, onRemove, customSymbols }: CustomSymbolAdderProps) {
  const [code, setCode] = useState('')
  const [name, setName] = useState('')
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [isLoadingName, setIsLoadingName] = useState(false)

  // 自动获取股票名称
  const autoFetchName = useCallback(async (inputCode: string) => {
    const normalizedCode = inputCode.trim()
    if (!normalizedCode) return
    
    // 验证格式
    if (!/^[\d]{6}$/.test(normalizedCode) && !/^sh\d{6}$/i.test(normalizedCode) && !/^sz\d{6}$/i.test(normalizedCode)) {
      return
    }
    
    setIsLoadingName(true)
    try {
      const fetchedName = await fetchStockName(normalizedCode)
      if (fetchedName) {
        setName(fetchedName)
      }
    } finally {
      setIsLoadingName(false)
    }
  }, [])

  const handleCodeChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const newCode = e.target.value
    setCode(newCode)
    
    // 当输入6位数字时自动获取名称
    if (/^[\d]{6}$/.test(newCode.trim())) {
      autoFetchName(newCode.trim())
    }
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setSuccess('')

    const normalizedCode = code.trim()
    if (!normalizedCode) {
      setError('请输入股票代码')
      return
    }

    // 验证股票代码格式
    if (!/^[\d]{6}$/.test(normalizedCode) && !/^sh\d{6}$/i.test(normalizedCode) && !/^sz\d{6}$/i.test(normalizedCode)) {
      setError('股票代码格式错误，请输入6位数字（如：601138）')
      return
    }

    const result = onAdd(normalizedCode, name.trim() || normalizedCode)
    if (result) {
      setSuccess(`已添加 ${normalizedCode}`)
      setCode('')
      setName('')
      setTimeout(() => setSuccess(''), 2000)
    } else {
      setError('该股票已在列表中')
    }
  }

  return (
    <div className="custom-symbol-adder">
      <form onSubmit={handleSubmit} className="adder-form">
        <div className="input-row">
          <input
            type="text"
            placeholder="股票代码（如：601138）"
            value={code}
            onChange={handleCodeChange}
            className="code-input"
            maxLength={8}
          />
          <input
            type="text"
            placeholder={isLoadingName ? "获取名称中..." : "名称（可选）"}
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="name-input"
            maxLength={10}
            disabled={isLoadingName}
          />
          <button type="submit" className="add-btn" disabled={isLoadingName}>添加</button>
        </div>
        {error && <span className="error-msg">{error}</span>}
        {success && <span className="success-msg">{success}</span>}
      </form>

      {customSymbols.length > 0 && (
        <div className="custom-list">
          <span className="list-label">自定义标的：</span>
          {customSymbols.map((sym) => (
            <span key={sym.code} className="custom-tag">
              {sym.name} ({sym.code})
              <button
                className="remove-btn"
                onClick={() => onRemove(sym.code)}
                title="删除"
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  )
}
