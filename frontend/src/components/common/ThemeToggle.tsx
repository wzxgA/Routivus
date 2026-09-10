import { otherTheme, themeLabel, type ThemeName } from '../../theme'

interface ThemeToggleProps {
  theme: ThemeName
  onToggle: () => void
}

export function ThemeToggle({ theme, onToggle }: ThemeToggleProps) {
  const current = themeLabel(theme)
  const target = themeLabel(otherTheme(theme))
  return (
    <button
      type="button"
      className="theme-toggle"
      onClick={onToggle}
      title={`当前${current}主题，点击切换到${target}`}
      aria-label={`当前${current}主题，点击切换到${target}`}
    >
      <span className="tt-orb" />
      {current}
    </button>
  )
}
