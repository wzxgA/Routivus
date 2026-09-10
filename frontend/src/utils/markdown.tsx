import { type ReactNode } from 'react'

// 轻量 Markdown 渲染：只支持会话流里实际出现的语法，
// 所有文本节点都由 React 转义，不引入 dangerouslySetInnerHTML，因此不存在 XSS 面。
// 链接仅允许 http/https，其余按纯文本输出。

function renderInline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = []
  const pattern = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\n]+\*)|(\[[^\]]+\]\([^)\s]+\))/g
  let lastIndex = 0
  let index = 0
  let match: RegExpExecArray | null
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) nodes.push(text.slice(lastIndex, match.index))
    const token = match[0]
    const key = `${keyPrefix}-i${index++}`
    if (token.startsWith('`')) {
      nodes.push(<code key={key}>{token.slice(1, -1)}</code>)
    } else if (token.startsWith('**')) {
      nodes.push(<strong key={key}>{token.slice(2, -2)}</strong>)
    } else if (token.startsWith('*')) {
      nodes.push(<em key={key}>{token.slice(1, -1)}</em>)
    } else {
      const link = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(token)
      if (link && /^https?:\/\//i.test(link[2])) {
        nodes.push(
          <a key={key} href={link[2]} target="_blank" rel="noreferrer noopener">
            {link[1]}
          </a>,
        )
      } else {
        nodes.push(token)
      }
    }
    lastIndex = match.index + token.length
  }
  if (lastIndex < text.length) nodes.push(text.slice(lastIndex))
  return nodes
}

interface Block {
  type: 'code' | 'heading' | 'ul' | 'ol' | 'quote' | 'hr' | 'para'
  level?: number
  lang?: string
  lines: string[]
}

function parseBlocks(source: string): Block[] {
  const lines = source.replace(/\r\n/g, '\n').split('\n')
  const blocks: Block[] = []
  let index = 0

  while (index < lines.length) {
    const line = lines[index]

    if (!line.trim()) {
      index += 1
      continue
    }

    const fence = /^```(\w*)\s*$/.exec(line.trim())
    if (fence) {
      const lang = fence[1]
      const body: string[] = []
      index += 1
      while (index < lines.length && !/^```\s*$/.test(lines[index].trim())) {
        body.push(lines[index])
        index += 1
      }
      index += 1 // 跳过收尾的 ```
      blocks.push({ type: 'code', lang, lines: body })
      continue
    }

    const heading = /^(#{1,6})\s+(.*)$/.exec(line)
    if (heading) {
      blocks.push({ type: 'heading', level: heading[1].length, lines: [heading[2]] })
      index += 1
      continue
    }

    if (/^(-{3,}|\*{3,})\s*$/.test(line.trim())) {
      blocks.push({ type: 'hr', lines: [] })
      index += 1
      continue
    }

    if (/^\s*[-*+]\s+/.test(line)) {
      const items: string[] = []
      while (index < lines.length && /^\s*[-*+]\s+/.test(lines[index])) {
        items.push(lines[index].replace(/^\s*[-*+]\s+/, ''))
        index += 1
      }
      blocks.push({ type: 'ul', lines: items })
      continue
    }

    if (/^\s*\d+[.)]\s+/.test(line)) {
      const items: string[] = []
      while (index < lines.length && /^\s*\d+[.)]\s+/.test(lines[index])) {
        items.push(lines[index].replace(/^\s*\d+[.)]\s+/, ''))
        index += 1
      }
      blocks.push({ type: 'ol', lines: items })
      continue
    }

    if (/^\s*>\s?/.test(line)) {
      const quote: string[] = []
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        quote.push(lines[index].replace(/^\s*>\s?/, ''))
        index += 1
      }
      blocks.push({ type: 'quote', lines: quote })
      continue
    }

    const para: string[] = []
    while (
      index < lines.length &&
      lines[index].trim() &&
      !/^(#{1,6})\s+/.test(lines[index]) &&
      !/^\s*[-*+]\s+/.test(lines[index]) &&
      !/^\s*\d+[.)]\s+/.test(lines[index]) &&
      !/^\s*>\s?/.test(lines[index]) &&
      !/^```/.test(lines[index].trim())
    ) {
      para.push(lines[index])
      index += 1
    }
    blocks.push({ type: 'para', lines: [para.join('\n')] })
  }

  return blocks
}

export function Markdown({ text }: { text: string }): ReactNode {
  const blocks = parseBlocks(text)
  return (
    <>
      {blocks.map((block, i) => {
        const key = `b${i}`
        switch (block.type) {
          case 'code':
            return (
              <pre key={key} className="md-code" data-lang={block.lang || undefined}>
                <code>{block.lines.join('\n')}</code>
              </pre>
            )
          case 'heading': {
            const Tag = (`h${Math.min(block.level ?? 1, 6)}`) as 'h1'
            return (
              <Tag key={key} className="md-heading">
                {renderInline(block.lines[0], key)}
              </Tag>
            )
          }
          case 'ul':
            return (
              <ul key={key} className="md-list">
                {block.lines.map((item, j) => (
                  <li key={`${key}-${j}`}>{renderInline(item, `${key}-${j}`)}</li>
                ))}
              </ul>
            )
          case 'ol':
            return (
              <ol key={key} className="md-list">
                {block.lines.map((item, j) => (
                  <li key={`${key}-${j}`}>{renderInline(item, `${key}-${j}`)}</li>
                ))}
              </ol>
            )
          case 'quote':
            return (
              <blockquote key={key} className="md-quote">
                {renderInline(block.lines.join('\n'), key)}
              </blockquote>
            )
          case 'hr':
            return <hr key={key} className="md-hr" />
          default:
            return (
              <p key={key} className="md-para">
                {renderInline(block.lines[0], key)}
              </p>
            )
        }
      })}
    </>
  )
}
