const configuredBase = (import.meta.env.VITE_API_BASE_URL || "").trim();
const apiBase = configuredBase.replace(/\/$/, "");

export const apiUrl = (path) => `${apiBase}${path}`;

export const apiFetch = (path, options) => fetch(apiUrl(path), options);

export async function readApiPayload(response) {
  if (typeof response.text === "function") {
    const text = await response.text();
    if (!text.trim()) {
      return {
        payload: null,
        problem: `服务端返回空响应（HTTP ${response.status}）`,
      };
    }
    try {
      return { payload: JSON.parse(text), problem: null };
    } catch {
      return {
        payload: null,
        problem: `服务端返回了非 JSON 响应（HTTP ${response.status}）`,
      };
    }
  }
  try {
    return { payload: await response.json(), problem: null };
  } catch {
    return {
      payload: null,
      problem: `服务端返回空响应或无效 JSON（HTTP ${response.status}）`,
    };
  }
}


export function runStopMessage(task) {
  if (task?.stop_reason === "difficulty_assessment_requires_repair") {
    return "独立难度评估未完成，题目等待从断点继续。请查看诊断中的具体错误。";
  }
  if (task?.stop_reason === "model_request_requires_repair") {
    return "本次生成因模型请求无法继续而暂停，已有结果已保存。若诊断中提示上下文超限，请更新并重启后端后从断点继续。";
  }
  if (task?.stop_reason === "user_paused") return "任务已暂停，可从断点继续。";
  return task?.stop_reason || "";
}
