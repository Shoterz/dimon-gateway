export const IDENTITY_VERIFICATION_FAILURE_MESSAGE =
  "Singpass identity verification wasn’t completed, so the order could not be approved. "
  + "Nothing was bought. Verify your identity and try again.";

export function isIdentityVerificationFailure(reasonCode) {
  return reasonCode === "KYC_NOT_COMPLETED" || reasonCode === "KYC_PENDING";
}
