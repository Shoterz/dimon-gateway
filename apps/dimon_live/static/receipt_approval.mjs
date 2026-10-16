// `signed` is {assertion, out_of_band_code?}. Retrying an interrupted approval
// resends the identical signed approval; the gateway returns the stored
// outcome instead of moving money again.
export async function requestReceiptApproval(choice, signed, fetchImpl = globalThis.fetch) {
  const url = `/api/receipts/${choice.receipt_id}/approve`;
  const options = {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({receipt_digest: choice.receipt_digest, ...signed}),
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
