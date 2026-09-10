import { themeLabel, type ThemeName } from '../../theme'

interface ThemeToggleProps {
  theme: ThemeName
  onToggle: () => void
}

export function ThemeToggle({ theme, onToggle }: ThemeToggleProps) {
  return (
    <button
      type="button"
      className="theme-toggle"
      onClick={onToggle}
      title={`切换到${themeLabel(theme)}主题`}
      aria-label={`切换到${themeLabel(theme)}主题`}
    >
      <span className="tt-orb" />
      {themeLabel(theme)}
    </button>
  )
}
