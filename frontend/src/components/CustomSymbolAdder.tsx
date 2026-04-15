import { useState, useCallback } from 'react'

interface CustomSymbolAdderProps {
  onAdd: (code: string, name: string) => boolean
  onRemove: (code: string) => void
  customSymbols: Array<{ code: string; name: string }>
}

async function fetchStockName(code: string): Promise<string | null> {
  try {
    const url = `/api/stock/name?code=${encodeURIComponent(code)}`
    console.log('[fetchStockName] 请求URL:', url)
    const resp = await fetch(url)
    console.log('[fetchStockName] 响应状态:', resp.status, resp.ok)
    if (!resp.ok) {
      console.log('[fetchStockName] 响应失败:', resp.statusText)
      return null
    }
    const text = await resp.text()
    console.log('[fetchStockName] 原始响应:', text)
    const data = JSON.parse(text)
    console.log('[fetchStockName] 解析后数据:', data)
    return data.name || null
  } catch (err) {
    console.error('[fetchStockName] 错误:', err)
    return null
  }
}

export function CustomSymbolAdder({ onAdd, onRemove, customSymbols }: CustomSymbolAdderProps) {
  const [code, setCode] = useState('')
  const [name, setName] = useState('')
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const [isLoadingName, setIsLoadingName] = useState(false)
  const [nameFetchStatus, setNameFetchStatus] = useState<'idle' | 'loading' | 'success' | 'error'>('idle')

  // 自动获取股票名称
  const autoFetchName = useCallback(async (inputCode: string) => {
    const normalizedCode = inputCode.trim()
    console.log('[CustomSymbolAdder] 开始获取名称:', normalizedCode)
    if (!normalizedCode) return
    
    // 验证格式
    if (!/^[\d]{6}$/.test(normalizedCode) && !/^sh\d{6}$/i.test(normalizedCode) && !/^sz\d{6}$/i.test(normalizedCode)) {
      console.log('[CustomSymbolAdder] 格式验证失败:', normalizedCode)
      return
    }
    
    setIsLoadingName(true)
    setNameFetchStatus('loading')
    try {
      const fetchedName = await fetchStockName(normalizedCode)
      console.log('[CustomSymbolAdder] 获取到名称:', fetchedName)
      if (fetchedName) {
        setName(fetchedName)
        setNameFetchStatus('success')
        console.log('[CustomSymbolAdder] 名称已设置:', fetchedName)
      } else {
        setName('') // 清空以便用户手动输入
        setNameFetchStatus('error')
        console.log('[CustomSymbolAdder] 未获取到名称')
      }
    } catch (err) {
      console.error('[CustomSymbolAdder] 获取名称失败:', err)
      setName('') // 出错时清空
      setNameFetchStatus('error')
    } finally {
      setIsLoadingName(false)
    }
  }, [])

  const handleCodeChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const newCode = e.target.value
    setCode(newCode)
    
    // 当输入改变时重置名称状态
    if (newCode.trim().length < 6) {
      setNameFetchStatus('idle')
      setName('')
    }
    
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
      setSuccess(`已添加 ${name.trim() || normalizedCode} (${normalizedCode})`)
      setCode('')
      setName('')
      setNameFetchStatus('idle')
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
            placeholder={
              nameFetchStatus === 'loading' 
                ? "🔍 获取名称中..." 
                : nameFetchStatus === 'success' 
                  ? "✓ 名称已自动填充" 
                  : "名称（可选）"
            }
            value={name}
            onChange={(e) => {
              setName(e.target.value)
              setNameFetchStatus('idle')
            }}
            className="name-input"
            maxLength={10}
            disabled={isLoadingName}
            readOnly={nameFetchStatus === 'success'}
            style={{
              borderColor: nameFetchStatus === 'success' ? '#4caf50' : undefined,
              backgroundColor: nameFetchStatus === 'success' ? 'rgba(76, 175, 80, 0.1)' : undefined,
              transition: 'all 0.3s',
              color: nameFetchStatus === 'success' ? '#4caf50' : undefined,
              fontWeight: nameFetchStatus === 'success' ? 'bold' : undefined
            }}
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
