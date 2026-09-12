import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { codeMeta, type HastNode } from '../../utils/hast'

interface CodeBlockProps {
  /** react-markdown 注入的 hast 节点（`pre`）。 */
  node?: unknown
  children?: ReactNode
}

/**
 * 代码块外壳：语言角标 + 一键复制。
 *
 * 着色不在这里做——高亮由 markdown 管线的 rehype 插件完成（见 utils/markdown.tsx），
 * 本组件只负责外壳与「降级后仍然可见」的部分：被跳过着色的块（过大、或语言不在
 * 白名单内）照样显示角标、可复制、可横向滚动。
 */
export function CodeBlock({ node, children }: CodeBlockProps) {
  const meta = codeMeta(node as HastNode | undefined)
  const [copied, setCopied] = useState(false)
  const timer = useRef<number | null>(null)

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current)
    },
    [],
  )

  const copy = useCallback(() => {
    const text = meta.text
    if (!text) return
    try {
      // 剪贴板可能不存在或被拒绝（非安全上下文 / 权限）：静默降级，不打断阅读。
      void navigator.clipboard
        .writeText(text)
        .then(() => {
          setCopied(true)
          if (timer.current !== null) window.clearTimeout(timer.current)
          timer.current = window.setTimeout(() => setCopied(false), 1200)
        })
        .catch(() => undefined)
    } catch {
      /* 忽略 */
    }
  }, [meta.text])

  return (
    <div className="md-code-wrap">
      <div className="md-code-bar">
        <span className="md-code-lang">{meta.lang || 'text'}</span>
        <button
          type="button"
          className={`md-code-copy${copied ? ' done' : ''}`}
          onClick={copy}
          title="复制代码"
        >
          {copied ? '已复制' : '复制'}
        </button>
      </div>
      <pre className="md-code">{children}</pre>
    </div>
  )
}
