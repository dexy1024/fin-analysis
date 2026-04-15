import { useState } from 'react'

interface CustomSymbolAdderProps {
  onAdd: (code: string, name: string) => boolean
  onRemove: (code: string) => void
  customSymbols: Array<{ code: string; name: string }>
}

export function CustomSymbolAdder({ onAdd, onRemove, customSymbols }: CustomSymbolAdderProps) {
  const [code, setCode] = useState('')
  const [name, setName] = useState('')
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

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
            onChange={(e) => setCode(e.target.value)}
            className="code-input"
            maxLength={8}
          />
          <input
            type="text"
            placeholder="名称（可选）"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="name-input"
            maxLength={10}
          />
          <button type="submit" className="add-btn">添加</button>
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
