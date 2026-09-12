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
  content: string
  thinking: string
}

export class StreamBatcher {
  private pending: StreamDelta = { content: '', thinking: '' }
  private handle: number | null = null

  constructor(
    private readonly flushTo: (delta: StreamDelta) => void,
    private readonly intervalMs: number = STREAM_FLUSH_MS,
  ) {}

  /** 收下一个 delta；首个 delta 会启动定时器，之后的都在攒批窗口内合并。 */
  push(kind: string, text: string): void {
    if (!text) return
    if (kind === 'thinking') this.pending.thinking += text
    else this.pending.content += text
    if (this.handle === null) {
      this.handle = window.setTimeout(() => this.flush(), this.intervalMs)
    }
  }

  /** 立即刷出攒下的内容；没有内容则什么也不做。 */
  flush(): void {
    this.clearTimer()
    const { content, thinking } = this.pending
    if (!content && !thinking) return
    this.pending = { content: '', thinking: '' }
    this.flushTo({ content, thinking })
  }

  /**
   * 丢弃攒下的内容并取消定时器。
   *
   * 用于"本轮完整内容已经由别的通道送达"（`message.completed`）或切换会话的场合：
   * 此时若还留着尾巴，等定时器到点就会把旧内容补到新会话里。
   */
  reset(): void {
    this.clearTimer()
    this.pending = { content: '', thinking: '' }
  }

  private clearTimer(): void {
    if (this.handle !== null) {
      window.clearTimeout(this.handle)
      this.handle = null
    }
  }
}
