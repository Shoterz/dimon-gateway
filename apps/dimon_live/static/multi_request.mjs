// Several requests in one sentence ("send Alicia $2, buy $100 of NVIDIA and
// add 88008800 to my PayNow"). The server drafts each part separately; this
// module keeps the list on screen and lets the user open each part's own
// receipt, order, or question. Every part is approved on its own; nothing
// here approves or submits anything.

const DONE = new Set([
  "EXECUTED_SYNTHETIC_TRANSFER", "EXECUTED_SYNTHETIC_BATCH", "EXECUTED_SYNTHETIC_TRADE",
  "EXECUTED_INVESTMENT_PLAN", "EXECUTED_PAYEE_ADDED", "ANSWERED", "PAPER_ORDER_FILLED",
]);

let plan = null;          // {steps: [{text, result, status}], active: number | null}
let followUp = null;      // index of the step whose follow-up request is in flight

export function stepStatus(result) {
  const outcome = result?.outcome;
  // A share purchase still asking how many, or waiting for its approval.
  if (result?.conversation?.status === "WAITING" || result?.paper_order) return {key: "waiting", label: "Needs you"};
  if (outcome === "PAPER_ORDER_RESULT") {
    const status = result.paper_order_result?.status;
    return status === "QUEUED" || status === "OPEN" ? {key: "done", label: "Approved · waiting"} : {key: "stopped", label: "Not completed"};
  }
  if (outcome === "ANSWERED") return {key: "done", label: "Answered"};
  if (DONE.has(outcome)) return {key: "done", label: "Done"};
  if (outcome === "BLOCKED" || outcome === "FAILED") return {key: "stopped", label: "Not drafted"};
  return {key: "waiting", label: "Needs you"};
}

function summary(result) {
  if (!result) return "";
  if (result.question) return result.question;
  if (result.answer) return result.answer;
  return (result.reason_code || result.outcome || "").replaceAll("_", " ").toLowerCase();
}

export function hasPlan() {
  return plan !== null;
}

export function resetPlan() {
  plan = null;
  followUp = null;
}

export function activeStepText() {
  return plan && plan.active !== null ? plan.steps[plan.active].text : null;
}

// A typed request that is not a follow-up of a step ends the plan.
export function noteSubmit() {
  if (followUp === null) plan = null;
}

// Re-run a step with words the user added ("... from everyday", "... as Uncle Tan").
export function markFollowUp() {
  if (plan && plan.active !== null) followUp = plan.active;
}

export function startPlan(result) {
  plan = {
    steps: result.steps.map(step => ({text: step.text, result: step.result, status: stepStatus(step.result)})),
    active: null,
  };
  followUp = null;
}

// Called for every rendered result while a plan is open.
export function observe(result) {
  if (!plan || result.outcome === "MULTI_REQUEST") return;
  if (followUp !== null) {
    const step = plan.steps[followUp];
    step.result = result;
    step.status = stepStatus(result);
    plan.active = followUp;
    followUp = null;
    return;
  }
  if (plan.active === null) return;
  const step = plan.steps[plan.active];
  // A stock conversation moves on with each answer; the step keeps the
  // latest turn so reopening it continues where the user left off.
  if (result.conversation || result.paper_order || result.paper_order_result) {
    step.result = result;
    step.status = stepStatus(result);
    return;
  }
  // Approval results mark the step done; refusals keep the draft so the
  // user can retry it.
  if (DONE.has(result.outcome)) step.status = stepStatus(result);
}

export function stepText(index) {
  return plan ? plan.steps[index]?.text ?? null : null;
}

export function openStep(index) {
  if (!plan) return null;
  plan.active = index;
  return plan.steps[index].result;
}

export function closeStep() {
  if (plan) plan.active = null;
}

export function renderList(root, escapeHtml, onOpen) {
  if (!plan) return;
  root.hidden = false;
  root.innerHTML = `<ol class="plan-list">${plan.steps.map((step, index) => `
    <li class="plan-step ${step.status.key}">
      <span class="plan-number">${index + 1}</span>
      <div>
        <strong>${escapeHtml(step.text)}</strong>
        <p>${escapeHtml(summary(step.result))}</p>
      </div>
      <span class="plan-status">${escapeHtml(step.status.label)}</span>
      ${step.status.key === "stopped" && !step.result?.question ? "" : `<button type="button" class="candidate-choice" data-step="${index}">${step.status.key === "waiting" ? "Review" : "View"}</button>`}
    </li>`).join("")}
  </ol>`;
  root.querySelectorAll("[data-step]").forEach(button => {
    button.addEventListener("click", () => onOpen(Number(button.dataset.step)));
  });
}

export function renderBar(bar, escapeHtml, onOpen, onAll) {
  if (!plan || plan.active === null) {
    bar.hidden = true;
    bar.innerHTML = "";
    return;
  }
  bar.hidden = false;
  const next = plan.steps.findIndex((step, index) => index !== plan.active && step.status.key === "waiting");
  bar.innerHTML = `
    <button type="button" class="plan-all">← All ${plan.steps.length} requests</button>
    <span>Request ${plan.active + 1} of ${plan.steps.length}</span>
    ${plan.steps.map((step, index) => `<button type="button" class="plan-dot ${step.status.key}${index === plan.active ? " current" : ""}" data-step="${index}" aria-label="Request ${index + 1}: ${escapeHtml(step.status.label)}">${index + 1}</button>`).join("")}
    ${plan.steps[plan.active].status.key === "done" && next >= 0 ? `<button type="button" class="plan-next" data-step="${next}">Next: request ${next + 1} →</button>` : ""}`;
  bar.querySelector(".plan-all").addEventListener("click", onAll);
  bar.querySelectorAll("[data-step]").forEach(button => {
    button.addEventListener("click", () => onOpen(Number(button.dataset.step)));
  });
}
