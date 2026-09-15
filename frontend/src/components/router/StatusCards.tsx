import type { RouterStatusView } from '../../api/types'

const SOURCE_TEXT: Record<string, string> = {
  semantic: '语义版产物',
  nosem: '无语义兜底产物',
  unavailable: '不可用',
}

/** 字节数：产物 1.8MB 这类量级，用 KB / MB 更好读。 */
function formatBytes(bytes: number): string {
  if (!bytes) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function when(value: string): string {
  return value ? value.replace('T', ' ').slice(0, 16) : '—'
}

function ratio(value: number | null): string {
  return value === null || value === undefined ? '—' : value.toFixed(3)
}

/**
 * 当前状态：ML 精判用的是哪一份产物、语义编码器可用不可用、上一版备份在不在。
 *
 * 这几项是"为什么路由表现不像预期"的第一现场，所以每一项都把**原因码**直接印出来，
 * 而不是只给一个绿点红点。
 */
export function StatusCards({ status }: { status: RouterStatusView }) {
  const { artifact, semantic, prev_artifact: prev } = status
  return (
    <div className="ri-grid">
      <div className="ri-card">
        <div className="ri-card-name">ML 精判产物</div>
        <div className="ri-big">
          <span className={`ri-badge ri-badge-${artifact.source}`}>
            {SOURCE_TEXT[artifact.source] ?? artifact.source}
          </span>
          {artifact.reason_code ? <span className="ri-code">{artifact.reason_code}</span> : null}
        </div>
        <dl className="ri-kv">
          <div>
            <dt>路径</dt>
            <dd className="mono" title={artifact.path}>
              {artifact.path || '—'}
            </dd>
          </div>
          <div>
            <dt>体积 / 时间</dt>
            <dd>
              {formatBytes(artifact.bytes)} · {when(artifact.mtime)}
            </dd>
          </div>
          <div>
            <dt>训练样本 / 验证准确率</dt>
            <dd>
              {artifact.n_samples ?? '—'} · {ratio(artifact.val_accuracy)}
              {artifact.trained_at ? ` · ${when(artifact.trained_at)}` : ''}
            </dd>
          </div>
          <div>
            <dt>语义列 / 语义头列宽</dt>
            <dd>
              {artifact.sem_dim} / {artifact.head_dim}
            </dd>
          </div>
          {artifact.eval_error ? (
            <div>
              <dt>内嵌字段</dt>
              <dd className="ri-warn">读取失败（{artifact.eval_error}）</dd>
            </div>
          ) : null}
        </dl>
      </div>

      <div className="ri-card">
        <div className="ri-card-name">语义编码器</div>
        <div className="ri-big">
          <span className={`ri-badge ri-badge-${semantic.available ? 'ok' : 'unavailable'}`}>
            {semantic.available ? '可用' : '不可用'}
          </span>
          {semantic.reason_code ? <span className="ri-code">{semantic.reason_code}</span> : null}
        </div>
        <dl className="ri-kv">
          <div>
            <dt>维度</dt>
            <dd>{semantic.dim || '—'}</dd>
          </div>
          <div>
            <dt>体积 / 时间</dt>
            <dd>
              {formatBytes(semantic.bytes)} · {when(semantic.mtime)}
            </dd>
          </div>
          <div>
            <dt>本进程编码次数 / 平均耗时</dt>
            <dd>
              {semantic.calls} 次{semantic.calls ? ` · ${semantic.avg_ms} ms` : ''}
            </dd>
          </div>
        </dl>
      </div>

      <div className="ri-card">
        <div className="ri-card-name">上一版产物（回滚用）</div>
        <div className="ri-big">
          <span className={`ri-badge ri-badge-${prev.present ? 'ok' : 'unavailable'}`}>
            {prev.present ? '在位' : '不存在'}
          </span>
        </div>
        <dl className="ri-kv">
          <div>
            <dt>体积 / 时间</dt>
            <dd>
              {formatBytes(prev.bytes)} · {when(prev.mtime)}
            </dd>
          </div>
          <div>
            <dt>来源</dt>
            <dd>{status.verified ? '读自已加载的共享资产（与对话同源）' : '按文件与依赖预判（未加载）'}</dd>
          </div>
          <div>
            <dt>重资产加载耗时</dt>
            <dd>{status.load_seconds ? `${status.load_seconds}s` : '—'}</dd>
          </div>
        </dl>
      </div>
    </div>
  )
}
