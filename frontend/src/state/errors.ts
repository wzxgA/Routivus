import { ApiError } from '../api/client'

/** 把任意异常转成可直接展示给用户的文案（优先服务端 error.message）。 */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.isNetworkError) return error.message
    return `${error.message}（${error.code}）`
  }
  if (error instanceof Error) return error.message
  return '未知错误'
}
