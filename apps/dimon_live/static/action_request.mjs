export async function requestAction(coordinates, fetchImpl = globalThis.fetch) {
  const options = {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      request_id: coordinates.requestId,
      text: coordinates.text,
    }),
  };

  try {
    const response = await fetchImpl("/api/actions", options);
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
