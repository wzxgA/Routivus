import * as api from '../../api'
import type { FileContent } from '../../api/types'
import { Markdown } from '../../utils/markdown'

const IMAGE_SUFFIX = /\.(png|jpe?g|gif|webp|svg|bmp|ico)$/i

const LANGUAGE_BY_SUFFIX: Record<string, string> = {
  ts: 'typescript',
  tsx: 'typescript',
  js: 'javascript',
  jsx: 'javascript',
  mjs: 'javascript',
  py: 'python',
  rs: 'rust',
  go: 'go',
  java: 'java',
  c: 'c',
  h: 'c',
  cpp: 'cpp',
  cc: 'cpp',
  hpp: 'cpp',
  json: 'json',
  yaml: 'yaml',
  yml: 'yaml',
  toml: 'ini',
  ini: 'ini',
  cfg: 'ini',
  sql: 'sql',
  sh: 'bash',
  bash: 'bash',
  ps1: 'bash',
  md: 'markdown',
  css: 'css',
  html: 'xml',
  xml: 'xml',
  svg: 'xml',
  diff: 'diff',
  patch: 'diff',
}

function languageOf(path: string): string {
  const suffix = path.slice(path.lastIndexOf('.') + 1).toLowerCase()
  return LANGUAGE_BY_SUFFIX[suffix] ?? 'plaintext'
}

/**
 * 选一段比正文里最长反引号串更长的围栏。
 *
 * 预览复用了会话正文的 Markdown 管线（自带语法着色、语言角标、超长块降级），
 * 所以要把文件内容包进代码围栏——正文里若本来就有 ``` 会提前闭合，围栏得加长。
 */
function fenceFor(content: string): string {
  let longest = 2
  for (const match of content.matchAll(/`+/g)) {
    longest = Math.max(longest, match[0].length)
  }
  return '`'.repeat(longest + 1)
}

interface FilePreviewProps {
  projectId: string
  file: FileContent
}

/** 只读预览：图片直接显示；文本走 Markdown 管线着色；二进制给提示。 */
export function FilePreview({ projectId, file }: FilePreviewProps) {
  if (file.binary && IMAGE_SUFFIX.test(file.path)) {
    return (
      <div className="fp-image">
        <img src={api.projectFileRawUrl(projectId, file.path)} alt={file.path} />
      </div>
    )
  }
  if (file.binary) {
    return <div className="hint">{file.reason || '二进制文件，不支持预览'}</div>
  }
  if (/\.mdx?$/i.test(file.path)) {
    return (
      <div className="fp-md">
        <Markdown text={file.content} />
      </div>
    )
  }
  const fence = fenceFor(file.content)
  return (
    <div className="fp-code">
      <Markdown text={`${fence}${languageOf(file.path)}\n${file.content}\n${fence}`} />
    </div>
  )
}
