/**
 * `@` 文件引用的前端解析（方案 05 §3.2）。
 *
 * 与后端 `routivus/server/files.py:parse_at_references` **保持同一套白名单规则**：
 * 只认「行首或空白 + `@` + 项目内相对路径」。前端这一份只服务**输入框表现层**
 * （高亮底纹、chip 列表、补全替换）；"模型到底被告知了哪些文件"永远以后端为准——
 * 即使两边规则漂移，最坏也只是高亮多画/少画一处，不会改变发给模型的内容。
 */

export interface AtRef {
  /** 项目内 POSIX 相对路径（未做存在性判断）。 */
  path: string
  /** `@` 在文本中的下标。 */
  start: number
  /** 路径结束下标（不含）。 */
  end: number
}

const MAX_REF_CHARS = 512

/**
 * `@` 前必须是行首、空白或标点；路径段不含空白、不含第二个 `@`、也不含标点。
 *
 * 标点边界与后端 `_REF_BOUNDARY` 逐字对应：`@a.ts、@b.ts` 是中文输入下的高频写法，
 * 标点若被吃进路径，chip 与高亮就会显示成 `a.ts、` 这种带尾巴的东西；而前置允许
 * 标点正是为了让顿号后面的那个 `@` 仍能被识别。
 */
const AT_REF_RE = /(^|[\s，。、；：！？（）【】《》“”‘’"',;:!?])@([^\s@，。、；：！？（）【】《》“”‘’"',;:!?]+)/g

/** 与后端 `_is_valid_ref` 逐条对应：`..`、绝对路径、引号转义一律不认。 */
function isValidRef(raw: string): boolean {
  if (!raw || raw.length > MAX_REF_CHARS) return false
  if (raw[0] === '"' || raw[0] === "'" || raw[0] === '/' || raw[0] === '\\') return false
  if (raw.includes('"') || raw.includes("'")) return false
  if (raw.replace(/\\/g, '/').split('/').some((part) => part === '..')) return false
  return true
}

/** 解析出全部 `@` 引用（含位置），按出现顺序。 */
export function parseAtRefs(text: string): AtRef[] {
  if (!text || !text.includes('@')) return []
  const out: AtRef[] = []
  AT_REF_RE.lastIndex = 0
  let match: RegExpExecArray | null
  while ((match = AT_REF_RE.exec(text)) !== null) {
    const raw = match[2]
    if (!isValidRef(raw)) continue
    const start = match.index + match[1].length
    out.push({ path: raw.replace(/\\/g, '/'), start, end: start + 1 + raw.length })
  }
  return out
}

/** 去重后的引用路径（保持出现顺序），chip 列表用。 */
export function uniqueRefPaths(text: string): string[] {
  const seen = new Set<string>()
  const paths: string[] = []
  for (const ref of parseAtRefs(text)) {
    if (seen.has(ref.path)) continue
    seen.add(ref.path)
    paths.push(ref.path)
  }
  return paths
}

export interface AtSegment {
  text: string
  ref: boolean
}

/** 把文本切成「普通片段 / `@`引用片段」，供高亮覆盖层渲染。 */
export function splitAtSegments(text: string): AtSegment[] {
  const refs = parseAtRefs(text)
  if (refs.length === 0) return text ? [{ text, ref: false }] : []
  const segments: AtSegment[] = []
  let cursor = 0
  for (const ref of refs) {
    if (ref.start > cursor) segments.push({ text: text.slice(cursor, ref.start), ref: false })
    segments.push({ text: text.slice(ref.start, ref.end), ref: true })
    cursor = ref.end
  }
  if (cursor < text.length) segments.push({ text: text.slice(cursor), ref: false })
  return segments
}

/**
 * 从文本里移除某个路径的**全部**引用（顺带吃掉紧随的一个空格，避免留下双空格）。
 *
 * 这是 A+B 方案里「文本是唯一真相」的关键：chip 列表由文本派生，删除动作最终
 * 也落回文本，两边不存在各自记账、互相漂移的可能。
 */
export function removeAtRef(text: string, path: string): string {
  const targets = parseAtRefs(text).filter((ref) => ref.path === path)
  let next = text
  for (let i = targets.length - 1; i >= 0; i -= 1) {
    const ref = targets[i]
    const end = next[ref.end] === ' ' ? ref.end + 1 : ref.end
    next = next.slice(0, ref.start) + next.slice(end)
  }
  return next
}
