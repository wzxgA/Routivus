/**
 * 流式文本的攒批器（性能方案 A，见 plans/enhancement/03-markdown-rendering.md §11）。
 *
 * 模型是一个 token 一个 token 吐的，每个 token 都会触发一次重型渲染（Markdown 解析 +
 * 元素构建），而这份成本随正文长度线性增长——于是"越写越慢"。这里把 delta 先攒起来、
 * 按固定间隔合并刷出，**把渲染次数封顶**，长回答不再随 token 速率恶化。
 *
 * 需要注意的局限：它只减少次数，**不降低单次成本**。超长回答仍然偏重，根治要靠
 * "已稳定前缀 + 活跃尾部"的拆分方案（B）。
 */

/** 攒批间隔。越大越省、文字越"跳"；60ms ≈ 每秒最多刷 16 次，肉眼接近连续。 */
export const STREAM_FLUSH_MS = 60

export interface StreamDelta {
  /**
   * 事件来源：主 ReAct 轮为 ""，/plan 子任务为 task:<id>，/team worker 为
   * agent:<id>（与后端段缓冲的分桶一致，方案 07 §4.2）。
   */
  source: string
  kind: 'content' | 'thinking'
  text: string
}

export class StreamBatcher {
  private pendingSource = ''
  private pendingKind: 'content' | 'thinking' | null = null
  private pendingText = ''
  private handle: number | null = null

  constructor(
    private readonly flushTo: (delta: StreamDelta) => void,
    private readonly intervalMs: number = STREAM_FLUSH_MS,
  ) {}

  /**
   * 收下一个 delta；首个 delta 会启动定时器，之后的都在攒批窗口内合并。
   *
   * 来源或 kind 变化时立即 flush：不同来源（并行 worker）、不同种类（思考 / 正文）
   * 的文本不允许黏进同一段——服务端的段边界就是按这两个维度划分的（方案 07 §4.8）。
   */
  push(source: string, kind: string, text: string): void {
    if (!text) return
    const normalized: 'content' | 'thinking' = kind === 'thinking' ? 'thinking' : 'content'
    if (this.pendingKind !== null && (this.pendingSource !== source || this.pendingKind !== normalized)) {
      this.flush()
    }
    this.pendingSource = source
    this.pendingKind = normalized
    this.pendingText += text
    if (this.handle === null) {
      this.handle = window.setTimeout(() => this.flush(), this.intervalMs)
    }
  }

  /** 立即刷出攒下的内容；没有内容则什么也不做。 */
  flush(): void {
    this.clearTimer()
    if (this.pendingKind === null || !this.pendingText) {
      this.pendingSource = ''
      this.pendingKind = null
      this.pendingText = ''
      return
    }
    const delta: StreamDelta = {
      source: this.pendingSource,
      kind: this.pendingKind,
      text: this.pendingText,
    }
    this.pendingSource = ''
    this.pendingKind = null
    this.pendingText = ''
    this.flushTo(delta)
  }

  /**
   * 丢弃攒下的内容并取消定时器。
   *
   * 带 source 时只丢弃**该来源**的尾巴（并行 worker 的另一路不受影响）；
   * 不带 source 时全部丢弃（切换会话 / 本轮结束的场合）。
   */
  reset(source?: string): void {
    if (source === undefined || this.pendingSource === source) {
      this.clearTimer()
      this.pendingSource = ''
      this.pendingKind = null
      this.pendingText = ''
    }
  }

  private clearTimer(): void {
    if (this.handle !== null) {
      window.clearTimeout(this.handle)
      this.handle = null
    }
  }
}
