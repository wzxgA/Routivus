import { useEffect, type ReactNode } from 'react'

interface ModalProps {
  title: string
  description?: string
  onClose: () => void
  children: ReactNode
  actions?: ReactNode
}

export function Modal({ title, description, onClose, children, actions }: ModalProps) {
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose])

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-title">{title}</div>
        {description ? <div className="modal-desc">{description}</div> : null}
        {children}
        {actions ? <div className="modal-actions">{actions}</div> : null}
      </div>
    </div>
  )
}
