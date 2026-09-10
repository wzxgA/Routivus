import { useMemo } from 'react'

interface Star {
  left: number
  top: number
  size: number
  duration: number
  opacity: number
  delay: number
}

/** 夜间主题的星空 + 流星背景层；暖白主题下由 CSS 隐藏。 */
export function Sky() {
  const stars = useMemo<Star[]>(() => {
    const result: Star[] = []
    for (let i = 0; i < 120; i += 1) {
      result.push({
        left: Math.random() * 100,
        top: Math.random() * 100,
        size: 0.8 + Math.random() * 1.8,
        duration: 2.4 + Math.random() * 3.6,
        opacity: 0.5 + Math.random() * 0.5,
        delay: Math.random() * 4,
      })
    }
    return result
  }, [])

  const meteors = useMemo(
    () =>
      Array.from({ length: 4 }, (_, index) => ({
        left: 30 + Math.random() * 65,
        top: Math.random() * 35,
        duration: 6.5 + Math.random() * 4,
        delay: index * 2.6,
      })),
    [],
  )

  return (
    <div className="sky" aria-hidden="true">
      {stars.map((star, index) => (
        <span
          key={`star-${index}`}
          className="star"
          style={{
            left: `${star.left}%`,
            top: `${star.top}%`,
            width: `${star.size}px`,
            height: `${star.size}px`,
            animationDuration: `${star.duration}s`,
            animationDelay: `${star.delay}s`,
            ['--so' as string]: String(star.opacity),
          }}
        />
      ))}
      {meteors.map((meteor, index) => (
        <span
          key={`meteor-${index}`}
          className="meteor"
          style={{
            left: `${meteor.left}%`,
            top: `${meteor.top}%`,
            animationDuration: `${meteor.duration}s`,
            animationDelay: `${meteor.delay}s`,
          }}
        />
      ))}
    </div>
  )
}
