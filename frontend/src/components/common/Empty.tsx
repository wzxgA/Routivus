interface EmptyProps {
  title: string
  hint?: string
  actionLabel?: string
  onAction?: () => void
}

export function Empty({ title, hint, actionLabel, onAction }: EmptyProps) {
  return (
    <div className="empty">
      <strong>{title}</strong>
      {hint ? <span>{hint}</span> : null}
      {actionLabel && onAction ? (
        <div style={{ marginTop: 10 }}>
          <button type="button" className="btn" onClick={onAction}>
            {actionLabel}
          </button>
        </div>
      ) : null}
    </div>
  )
}
