import { useEffect, useState } from 'react'
import * as api from '../../api'
import type { Project, Session } from '../../api/types'
import { describeError } from '../../state/workspace'

interface ConfigViewProps {
  projects: Project[]
}

interface ProjectRuntime {
  projectId: string
  projectName: string
  session: Session | null
  error: string | null
}

/**
 * 配置视图。
 *
 * 后端当前只暴露项目 / 会话 / 笔记 / 活动的 REST 与 WebSocket，
 * 没有 `/api/config`（Provider 列表、SmartRouter 四档、HITL 开关）接口，
 * 因此这里展示可获得的运行时信息，并明确标注缺失的接口。
 */
export function ConfigView({ projects }: ConfigViewProps) {
  const [runtime, setRuntime] = useState<ProjectRuntime[]>([])

  useEffect(() => {
    let cancelled = false
    async function load() {
      const results = await Promise.all(
        projects.map(async (project) => {
          try {
            const session = await api.getCurrentSession(project.id)
            return { projectId: project.id, projectName: project.name, session, error: null }
          } catch (err) {
            return {
              projectId: project.id,
              projectName: project.name,
              session: null,
              error: describeError(err),
            }
          }
        }),
      )
      if (!cancelled) setRuntime(results)
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [projects])

  return (
    <section className="view view-page">
      <div className="page-title">配置</div>
      <div className="page-sub">Provider、模型与安全策略（只读；写入能力待后端配置接口）</div>

      <div className="big-card">
        <div className="card-name">Provider 与模型</div>
        <div className="card-desc">按项目列出最近会话的运行时 provider / model</div>
        <table className="table">
          <thead>
            <tr>
              <th>项目</th>
              <th>Provider</th>
              <th>模型</th>
              <th>会话状态</th>
            </tr>
          </thead>
          <tbody>
            {runtime.map((item) => (
              <tr key={item.projectId}>
                <td>{item.projectName}</td>
                <td className="mono">{item.session?.active_provider ?? '—'}</td>
                <td className="mono">{item.session?.active_model ?? '—'}</td>
                <td>{item.session ? item.session.status : (item.error ?? '无会话')}</td>
              </tr>
            ))}
            {runtime.length === 0 ? (
              <tr>
                <td colSpan={4}>暂无项目</td>
              </tr>
            ) : null}
          </tbody>
        </table>
        <div className="hint">
          Provider 的增删改查与模型切换由后端 `/provider`、`/model` 命令完成；Web Console 需要一个新的
          配置接口才能在此直接编辑（当前服务端仅提供项目 / 会话 / 笔记 / 活动端点）。
        </div>
      </div>

      <div className="big-card">
        <div className="card-name">SmartRouter 四档</div>
        <div className="card-desc">Basic / Enhanced / Superior / Ultimate 的 provider 与 model 映射</div>
        <div className="hint">尚未接入：需要后端暴露 `/api/config/smart-router` 之类的只读快照接口。</div>
      </div>

      <div className="big-card">
        <div className="card-name">安全策略</div>
        <div className="card-desc">审批与策略层由服务端强制，客户端无法绕过</div>
        <table className="table">
          <thead>
            <tr>
              <th>项</th>
              <th>行为</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>HITL 审批</td>
              <td>默认 `execute_command` 必审、`write_file` 确认；由会话 WebSocket 推送审批卡</td>
            </tr>
            <tr>
              <td>路径 / 命令策略</td>
              <td>`PathGuard`（含 symlink 与 cwd 越界）与 `CommandGuard` 直接拒绝，不可被审批放行绕过</td>
            </tr>
            <tr>
              <td>终端通道</td>
              <td>服务端绑定项目根目录启动；需配置 Token 或 Origin，否则端点直接拒绝</td>
            </tr>
            <tr>
              <td>审计</td>
              <td>工具调用、审批、终端命令写入项目 `.routivus/audit.log`（敏感字段脱敏）</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div className="big-card">
        <div className="card-name">技能</div>
        <div className="card-desc">Skill 发现与按需加载</div>
        <div className="hint">尚未接入：需要后端暴露 Skill 列表与启用状态接口。</div>
      </div>
    </section>
  )
}
