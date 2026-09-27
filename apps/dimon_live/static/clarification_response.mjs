export async function requestClarificationResponse(
  clarification,
  requestId,
  text,
  fetchImpl = globalThis.fetch,
) {
  const url = `/api/clarifications/${clarification.clarification_id}/respond`;
  const options = {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({request_id: requestId, text}),
  };

  try {
    const response = await fetchImpl(url, options);
    if (!response.ok) {
      return {
        kind: "UNKNOWN",
        message: `Gateway returned HTTP ${response.status}`,
      };
    }
    return {kind: "RESULT", result: await response.json()};
  } catch (error) {
    return {
      kind: "UNKNOWN",
      message: error instanceof Error ? error.message : String(error),
    };
  }
}
