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
  applyFavicon(theme)
}

/**
 * 标签页图标跟随主题。
 *
 * 夜间主题下仍挂日间版图标，浏览器标签栏与任务栏上就是一枚扎眼的浅色方块；
 * 两版都是 SVG，所以只换同一个 `<link>` 的 href（元素在 index.html 里预置）。
 */
function applyFavicon(theme: ThemeName): void {
  const link = document.getElementById('app-favicon')
  if (!(link instanceof HTMLLinkElement)) return
  link.href = theme === 'night' ? '/routivus-logo-night.svg' : '/routivus-logo.svg'
}

export function otherTheme(theme: ThemeName): ThemeName {
  return theme === 'light' ? 'night' : 'light'
}

/** 按钮文案显示**当前**主题（与原型 `ttLabel.textContent = THEME_NAMES[theme]` 一致）。 */
export function themeLabel(theme: ThemeName): string {
  return theme === 'light' ? '日间' : '夜间'
}
