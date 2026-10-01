/** 从 Axios 风格错误对象中提取服务端 message；取不到时回退 fallback */
export function getErrorMessage(error: unknown, fallback: string): string {
  const err = error as { response?: { data?: { message?: string } }; message?: string };
  return err.response?.data?.message || err.message || fallback;
}
