# Dimon banking home redesign

## Status

Approved design, pending implementation.

## Goal

Reshape Dimon Live from a settlement-console-first demo into a credible
synthetic consumer-banking experience. The default view should help a user
understand fictional accounts, start common banking tasks, and ask Dimon for
help without exposing implementation details. Dimon's existing deterministic
grounding, receipt, signing, identity, risk, and exactly-once controls remain
the only path to an actionable result.

This interface is inspired by the information hierarchy of DBS Home but is not
a DBS replica. It uses no DBS customer data, proprietary assets, or connection
to real payment rails.

## Users and success criteria

The primary audience is hackathon judges and teammates evaluating whether an
AI assistant can sit naturally inside a banking product without weakening
transaction controls.

The redesign succeeds when a user can:

- recognise the default view as a normal mobile banking home;
- see clearly fictional account balances and common banking shortcuts;
- begin a typed or spoken request from Home or the focused assistant view;
- route requests to payments, investing, account information, or service
  requests through the existing action API;
- review and explicitly approve every actionable request using the existing
  security flow;
- inspect technical evidence only when they deliberately open Evidence; and
- operate the essential experience with keyboard, touch, or assistive
  technology on mobile and desktop.

## Product boundaries

The redesign changes presentation and client-side navigation. It does not
replace or relax the backend's authority.

- All displayed balances, people, investments, activity, and offers are
  synthetic demo data returned by or aligned with the existing backend.
- The browser does not acquire authority to choose account identifiers,
  resolve recipients, determine eligibility, construct approval facts, or
  execute transactions.
- Payment and investment actions continue through stored receipts or orders,
  deterministic validation, digest-bound approval, device/face confirmation,
  Singpass where applicable, risk checks, and idempotent execution.
- Voice remains an input method, never an approval signal.
- No real banking operation or real DBS balance is introduced.
- Existing API contracts remain compatible. New presentation helpers may be
  introduced only when they derive from public, session-scoped state.

## Architecture

The implementation keeps the current FastAPI application and framework-free
HTML, CSS, and JavaScript modules. A small client-side application shell owns
view selection while existing action, conversation, voice, investing, and
approval modules continue to own their specialised behavior.

```text
FastAPI session-scoped APIs
        |
        v
existing action/conversation/approval modules
        |
        v
client application shell
  |-- Home
  |-- Pay entry
  |-- Ask Dimon
  |-- Activity
  |-- More / Evidence
  `-- shared approval dialogs
```

Navigation is state-driven rather than URL-dependent so refresh and direct
loading retain the current deployment model. The shell uses semantic sections,
updates the active navigation state, moves focus appropriately, and does not
duplicate transaction logic.

## Information architecture

### Persistent application shell

The shell contains a compact top bar and a dark five-item bottom navigation:

1. Home
2. Pay
3. Ask
4. Activity
5. More

Ask is visually prominent because it is the product's distinctive capability,
but it remains a standard accessible button. Each item uses the same outlined
SVG icon family with consistent stroke width, view box, optical size, and
active treatment. Emoji and Unicode pictograms are not used as interface
icons.

The synthetic-demo boundary remains visible in the shell as concise text. It
does not compete with the primary banking tasks.

### Home

Home contains, in order:

- a red promotional header with restrained copy and a dismissible or static
  demo offer that makes no real product claim;
- greeting and notification controls;
- a balance summary sourced from current synthetic account state;
- white account cards on a pale cool-gray background, with account label,
  masked synthetic identifier, and SGD balance;
- shortcuts for Pay & transfer, Scan & pay, Bills, Cards, Invest, and More;
- a persistent Ask Dimon text and microphone composer;
- recent synthetic activity derived from committed ledger state; and
- clear empty and loading states.

Account cards and shortcuts behave as navigation or request starters. They do
not directly execute actions.

### Ask Dimon

The focused assistant view presents a short conversation history, suggested
prompts, a live result region, and the same text/microphone composer used on
Home. The assistant is a task surface rather than a decorative chatbot.

The existing `/api/actions` response remains the routing authority. The client
maps response views and intent types into four presentation families:

- payments: recipient grounding, clarification, receipt review, and approval;
- investing: portfolio information, research, plans, profile, and paper-order
  review;
- account information: balances, holdings, and recent activity rendered
  inline; and
- requests: supported service responses or a bounded explanation when a
  request is unavailable.

The existing conversation endpoints continue to handle multi-turn investment
and clarification flows. Client routing changes presentation only and must not
infer execution authority from free text.

### Pay, Activity, and More

Pay opens a familiar task launcher for PayNow, saved beneficiaries, transfers,
and bills. Each launcher places a suitable request in Ask Dimon or opens the
existing bounded action flow; none bypass receipt creation.

Activity shows committed synthetic transactions in consumer-readable form.
Technical IDs remain available in Evidence rather than dominating this view.

More groups profile, security, investor profile, demo reset, and Evidence.
Reset remains explicit and clearly scoped to the synthetic session.

### Evidence and Presenter

Evidence is an optional technical view for judges and developers. It contains:

- public inference mode and local health status;
- the deterministic execution trace;
- current receipt or order coordinates;
- the committed ledger with transaction identifiers;
- fixture/demo playbook controls;
- audit or validation details already exposed safely by the client; and
- a link to the existing Presenter view.

Internal URLs, credentials, filesystem paths, hidden reasoning, and private
model configuration remain undisclosed.

## Action review and approval

An actionable response transitions into an inline review panel or existing
approval dialog. The review shows the stored, server-owned facts appropriate
to the action: source, recipient or instrument, amount, currency, fees when
present, reference, and risk or market warnings.

The primary action uses specific language such as "Review transfer" or
"Confirm paper order." Approval controls remain disabled until their existing
prerequisites are satisfied. Cancel always leaves the ledger unchanged.

The redesign may improve labels, layout, and responsive behavior around these
controls but must not reconstruct receipts in the browser, remove digest
binding, skip identity checks, auto-check acknowledgements, auto-submit an
approval, or alter exactly-once behavior.

## Visual system

The interface uses a restrained product palette:

- Dimon red: `#C9152C` for the promotional header and primary emphasis;
- deep red: `#9D1023` for pressed and high-contrast states;
- graphite: `#17191D` for bottom navigation and primary text;
- cool gray: `#F2F3F5` for the application background;
- white: `#FFFFFF` for account and task surfaces;
- slate: `#60646C` for secondary text, adjusted where needed for WCAG contrast.

Typography uses the existing system sans stack with a compact mobile-banking
scale. Cards use 12–16 px radii and either a subtle border or a restrained
shadow, not both decoratively. Red is reserved for brand and primary actions;
success, warning, and error retain distinct semantic treatments and text/icon
labels.

Motion is limited to 150–250 ms state transitions for navigation, review
expansion, and feedback. Reduced-motion preferences remove nonessential
transforms.

## Responsive and accessible behavior

- Mobile is the primary layout. The five-item navigation remains fixed above
  safe-area insets and never covers the composer or content.
- Desktop centers a banking-sized content column; assistant and Evidence may
  use a wider two-column layout when space permits.
- Every icon-only control has an accessible name and visible focus state.
- View changes move focus to the destination heading without causing a scroll
  trap.
- Dialogs retain native focus trapping and Escape behavior.
- Live transcript, action result, voice status, and approval status use
  appropriately scoped live regions.
- Text and controls meet WCAG AA contrast; status never depends on color alone.
- Loading uses skeletons and disabled controls, while empty states explain the
  next useful action.

## Error handling

Existing stable backend errors remain authoritative. The shell translates them
into task-level guidance without hiding reason codes from Evidence.

- Model or voice unavailable: typed input remains usable and the ledger stays
  unchanged.
- Ambiguous or incomplete intent: the assistant asks only for the missing fact
  through the existing clarification flow.
- Expired receipt or changed market data: approval is blocked and the user is
  prompted to refresh or start again.
- Identity or approval failure: the review remains non-mutating and explains
  the allowed retry path.
- Network failure: the submitted text remains visible for retry and no success
  state is inferred.

## Implementation scope

Expected changes are concentrated in:

- `apps/dimon_live/static/index.html` for the application shell and views;
- `apps/dimon_live/static/app.css` for tokens, responsive layout, components,
  and interaction states;
- `apps/dimon_live/static/app.js` for navigation, assistant presentation, and
  intent/view routing;
- focused static JavaScript modules when extracting navigation or icons makes
  behavior independently testable;
- `apps/dimon_live/app.py` only when a new static module must be served; and
- frontend contract and behavior tests under `apps/dimon_live/tests/`.

Backend payment, investing, approval, signing, session, risk, and ledger
modules are out of scope unless a failing compatibility test exposes a genuine
presentation integration defect.

## Verification

Implementation is complete only when verification demonstrates:

1. Home exposes fictional accounts, shortcuts, recent activity, the persistent
   Ask Dimon composer, and a dark five-item bottom navigation.
2. All interface icons come from one SVG line-icon vocabulary; no Unicode
   pictograms remain in application controls.
3. Text and microphone submission work from Home and the focused assistant.
4. Existing payment, clarification, bill, investing, and paper-order responses
   route to the correct presentation without changing API authority.
5. Actionable requests still require the existing exact review and approval
   sequence, including identity and risk controls where applicable.
6. Activity reflects only committed synthetic state.
7. Evidence contains fixture, trace, ledger, playbook, and Presenter access but
   is not part of the default Home experience.
8. Keyboard focus, live regions, dialog behavior, reduced motion, and mobile
   safe-area spacing work as specified.
9. Existing Dimon backend and security tests continue to pass.
10. Browser inspection covers Home, Ask, review, Activity, and Evidence at
    representative mobile and desktop widths.

## Non-goals

This work does not add real DBS branding, personal balances, real PayNow or
bank connectivity, live trading, a new authentication system, a new frontend
framework, general-purpose customer support, or autonomous execution.
