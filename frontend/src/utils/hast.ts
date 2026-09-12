/**
 * hast 节点的小工具（Markdown 渲染管线用）。
 *
 * react-markdown 的自定义组件拿到的是 hast 节点。这里只做三件事：拼文本、读 class、
 * 从 `pre` 里取出「语言 + 源码」，供代码块组件（复制按钮）与降级插件（判大小）共用。
 * 不引第三方工具——代码块内部没有嵌套结构，递归拼文本足够。
 */

export interface HastNode {
  type: string
  value?: string
  tagName?: string
  properties?: Record<string, unknown>
  children?: HastNode[]
}

export function hastChildren(node: HastNode | undefined): HastNode[] {
  return Array.isArray(node?.children) ? node.children : []
}

/** 拼接节点下的全部文本（换行原样保留，供复制使用）。 */
export function hastText(node: HastNode): string {
  if (node.type === 'text') return node.value ?? ''
  return hastChildren(node).map(hastText).join('')
}

export function hastClasses(node: HastNode): string[] {
  const value = node.properties?.className
  if (!Array.isArray(value)) return []
  return value.filter((item): item is string => typeof item === 'string')
}

export function isElement(node: HastNode | undefined, tagName: string): boolean {
  return Boolean(node && node.type === 'element' && node.tagName === tagName)
}

export interface CodeMeta {
  /** 语言名（`language-xxx` 的 xxx，或被降级插件搬到 `data-lang` 的值）；未知为空串。 */
  lang: string
  text: string
}

/**
 * 从 `pre` 节点取出代码块的元信息。
 *
 * 语言有两个来源：常规情况下是 `rehype-highlight` 认的 `language-xxx` class；被判定为
 * 「太大不值得着色」的块会丢掉 class、改用 `data-lang`（见 utils/markdown.tsx 的预扫插件），
 * 这样角标仍然显示语言，只是没有颜色。
 */
export function codeMeta(pre: HastNode | undefined): CodeMeta {
  const code = hastChildren(pre).find((child) => isElement(child, 'code'))
  if (!code) return { lang: '', text: '' }
  const langClass = hastClasses(code).find((name) => name.startsWith('language-'))
  const dataLang = code.properties?.dataLang
  const lang =
    typeof dataLang === 'string' && dataLang
      ? dataLang
      : (langClass?.slice('language-'.length) ?? '')
  return { lang, text: hastText(code) }
}
