const SAFE_ERROR_NAME = /^[A-Za-z0-9_.-]{1,64}$/;

export function describeVoiceFailure(stage, error) {
  const errorName = SAFE_ERROR_NAME.test(error?.name || "") ? error.name : "UnknownError";
  let status = "Voice could not start · reload and try again";
  if (errorName === "NotAllowedError" || errorName === "SecurityError") {
    status = "Microphone access denied · allow it in this website's settings";
  } else if (errorName === "NotFoundError") {
    status = "No microphone was found · use typed input";
  } else if (stage === "worklet_module" || stage === "worklet_node") {
    status = "iPhone audio processor failed · retry once after reloading";
  } else if (stage === "socket") {
    status = "Voice connection failed · reload and try again";
  }
  return {stage, errorName, status};
}

export function reportVoiceFailure(details, fetchImpl = fetch) {
  const body = {
    stage: details.stage,
    error_name: SAFE_ERROR_NAME.test(details.errorName || "") ? details.errorName : "UnknownError",
  };
  if (typeof details.requestId === "string" && /^[0-9a-f-]{36}$/i.test(details.requestId)) {
    body.request_id = details.requestId;
  }
  return fetchImpl("/api/voice/diagnostics", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
    keepalive: true,
  }).catch(() => undefined);
}
