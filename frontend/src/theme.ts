export type ThemeName = 'light' | 'night'

const STORAGE_KEY = 'routivus-theme'

export function readStoredTheme(): ThemeName {
  const stored = window.localStorage.getItem(STORAGE_KEY)
  if (stored === 'night' || stored === 'light') return stored
  return 'light'
}

export function applyTheme(theme: ThemeName): void {
  document.documentElement.setAttribute('data-theme', theme)
  window.localStorage.setItem(STORAGE_KEY, theme)
}

export function otherTheme(theme: ThemeName): ThemeName {
  return theme === 'light' ? 'night' : 'light'
}

export function themeLabel(theme: ThemeName): string {
  return theme === 'light' ? '夜间' : '日间'
}
