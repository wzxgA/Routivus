import { memo } from 'react'
import ReactMarkdown, { type Components, type Options } from 'react-markdown'
import rehypeHighlight from 'rehype-highlight'
import remarkGfm from 'remark-gfm'
import bash from 'highlight.js/lib/languages/bash'
import c from 'highlight.js/lib/languages/c'
import cpp from 'highlight.js/lib/languages/cpp'
import css from 'highlight.js/lib/languages/css'
import diff from 'highlight.js/lib/languages/diff'
import dockerfile from 'highlight.js/lib/languages/dockerfile'
import go from 'highlight.js/lib/languages/go'
import ini from 'highlight.js/lib/languages/ini'
import java from 'highlight.js/lib/languages/java'
import javascript from 'highlight.js/lib/languages/javascript'
import json from 'highlight.js/lib/languages/json'
import markdown from 'highlight.js/lib/languages/markdown'
import plaintext from 'highlight.js/lib/languages/plaintext'
import python from 'highlight.js/lib/languages/python'
import rust from 'highlight.js/lib/languages/rust'
import sql from 'highlight.js/lib/languages/sql'
import typescript from 'highlight.js/lib/languages/typescript'
import xml from 'highlight.js/lib/languages/xml'
import yaml from 'highlight.js/lib/languages/yaml'
import { CodeBlock } from '../components/common/CodeBlock'
import { hastChildren, hastClasses, hastText, isElement, type HastNode } from './hast'

/*
 * 会话正文的 Markdown 渲染。
 *
 * 完整设计与取舍见 plans/enhancement/03-markdown-rendering.md，要点：
 * - 用 react-markdown（不产生 innerHTML，没有 XSS 面；裸 HTML 不渲染）
 * - remark-gfm 补上表格 / 任务列表 / 删除线 / 自动链接
 * - 高亮只在「非流式」时跑：流式期间每一帧都会重渲染，逐个 token 重涂一遍颜色太贵
 * - 过大的代码块跳过着色（预扫插件），保住滚动与内存
 * - 配色全部走 CSS 变量（styles/app.css），所以切换主题不需要重渲染历史消息
 */

/** 只注册会话里真会出现的语言：highlight.js 全量语言包会让首屏产物明显变大。 */
const LANGUAGES = {
  bash,
  c,
  cpp,
  css,
  diff,
  dockerfile,
  go,
  ini,
  java,
  javascript,
  json,
  markdown,
  plaintext,
  python,
  rust,
  sql,
  typescript,
  xml,
  yaml,
}

/** 超过任一阈值就不着色（只保留等宽、角标、复制、横向滚动）。 */
const MAX_HIGHLIGHT_LINES = 300
const MAX_HIGHLIGHT_CHARS = 20_000

/**
 * 预扫：把过大的代码块从「待着色」降级成纯文本。
 *
 * 做法是把 `language-xxx` 换成 `data-lang`——rehype-highlight 只认 `language-` 前缀，
 * 于是自然跳过它；语言角标仍能从 `data-lang` 读出（utils/hast.ts 的 codeMeta）。
 * 这样不必自己实现高亮逻辑，跳过一块只是"把标记换个地方"。
 */
function rehypeSkipLargeCode() {
  return (tree: HastNode) => {
    const walk = (node: HastNode) => {
      if (node.type === 'element' && node.tagName === 'pre') {
        const code = hastChildren(node).find((child) => isElement(child, 'code'))
        const classes = code ? hastClasses(code) : []
        const langClass = classes.find((name) => name.startsWith('language-'))
        if (code && langClass) {
          const text = hastText(code)
          if (text.length > MAX_HIGHLIGHT_CHARS || text.split('\n').length > MAX_HIGHLIGHT_LINES) {
            code.properties = {
              ...code.properties,
              className: classes.filter((name) => name !== langClass),
              dataLang: langClass.slice('language-'.length),
            }
          }
        }
      }
      for (const child of hastChildren(node)) walk(child)
    }
    walk(tree)
  }
}

/* 组件与插件都用模块级常量：它们的身份变化会让 react-markdown 重建解析器（等于每次
   渲染都重新解析整段 markdown），必须保持稳定。 */
const COMPONENTS: Components = {
  pre: CodeBlock,
  // 表格自带横向滚动容器：会话列宽有限，宽表格不能撑破布局
  table: ({ children }) => (
    <div className="md-table-wrap">
      <table className="md-table">{children}</table>
    </div>
  ),
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noreferrer noopener">
      {children}
    </a>
  ),
  // 图片按 markdown 语义渲染，但不带 referrer、懒加载、限宽（模型给出的 URL 不可信）
  img: ({ src, alt }) => (
    <img className="md-img" src={src} alt={alt ?? ''} loading="lazy" referrerPolicy="no-referrer" />
  ),
}

const REMARK_PLUGINS: Options['remarkPlugins'] = [remarkGfm]
const REHYPE_PLAIN: Options['rehypePlugins'] = []
const REHYPE_HIGHLIGHT: Options['rehypePlugins'] = [
  rehypeSkipLargeCode,
  [rehypeHighlight, { languages: LANGUAGES, detect: false, ignoreMissing: true }],
]

interface MarkdownProps {
  text: string
  /** 流式追加中：此时不着色（每帧都要重涂一遍，太贵），等这一轮说完再上色。 */
  streaming?: boolean
}

function MarkdownImpl({ text, streaming = false }: MarkdownProps) {
  return (
    <ReactMarkdown
      remarkPlugins={REMARK_PLUGINS}
      rehypePlugins={streaming ? REHYPE_PLAIN : REHYPE_HIGHLIGHT}
      components={COMPONENTS}
    >
      {text}
    </ReactMarkdown>
  )
}

/**
 * memo 很关键：流式追加时父组件每一帧都会渲染，只有正文真的变了才需要重新解析。
 */
export const Markdown = memo(MarkdownImpl)
