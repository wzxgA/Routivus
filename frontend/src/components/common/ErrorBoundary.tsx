import { Component, type ErrorInfo, type ReactNode } from 'react'

interface ErrorBoundaryProps {
  children: ReactNode
  /** 出错时告诉用户是哪一块坏了（如「消息流」）。 */
  label?: string
}

interface ErrorBoundaryState {
  error: Error | null
}

/**
 * 渲染错误兜底：任何一块 UI 抛异常都降级为局部错误卡，而不是把整棵 React 树
 * 卸载成一张纯底色页（ask 卡曾因契约错位把整个应用打崩，代价是用户连"取消"
 * 都点不到——本组件就是那次教训的产物）。
 *
 * 用 class 组件是唯一写法：错误边界必须是带 getDerivedStateFromError /
 * componentDidCatch 的 class。
 */
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // 留一份带组件栈的现场：用户报障时能直接对照。
    console.error('[routivus] 渲染失败', this.props.label ?? '', error, info.componentStack)
  }

  private reset = () => {
    this.setState({ error: null })
  }

  render(): ReactNode {
    const { error } = this.state
    if (error === null) return this.props.children
    return (
      <div className="approval" role="alert">
        <div className="approval-title">
          <span className="approval-risk">渲染失败</span>
          {this.props.label ?? '这块界面出了问题'}
        </div>
        <div className="ask-label mono" style={{ whiteSpace: 'pre-wrap' }}>
          {String(error.message || error)}
        </div>
        <div className="approval-actions">
          <button type="button" className="btn" onClick={this.reset}>
            重试
          </button>
        </div>
        <div className="approval-note">
          其余功能不受影响；若反复出现，请把上面的错误信息连同日志一起反馈。
        </div>
      </div>
    )
  }
}
