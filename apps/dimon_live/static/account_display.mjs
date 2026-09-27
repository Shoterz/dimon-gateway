export function accountDisplay(account) {
  if (account.paynow_proxy) {
    return {
      ownerLabel: "Synthetic PayNow",
      proxyLabel: `PayNow demo · ${account.paynow_proxy}`,
    };
  }
  return {
    ownerLabel: account.owner_display_name,
    proxyLabel: null,
  };
}
