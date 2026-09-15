/**
 * 4 段档位电量条（方案 14 §4.2）：1–4 格对应 Basic–Ultimate，高度递增
 * （信号强度隐喻），档位高低不用识字就能感知。
 *
 * 颜色不在这里写死：由父级 `.tiered[data-tier]` 注入 `--tier-color`，
 * 未点亮格恒 18% 透明度、点亮格走档位色。`lightup` 只在档位变化时挂一次
 * （逐格点亮、每格错 60ms），播完由父级把 motion 类清空，下次变化才能重触发。
 */
export function TierLevel({ level, lightup = false }: { level: number; lightup?: boolean }) {
  const safe = Math.max(0, Math.min(4, Math.round(level)))
  return (
    <span className={`segs${lightup ? ' lightup' : ''}`} data-level={safe} aria-hidden="true">
      <i />
      <i />
      <i />
      <i />
    </span>
  )
}
