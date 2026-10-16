# Next implementation

## Authentication rules

**Status: Verifying**

- For utility payments and transfers to friends under **$200**, use voice
  recognition.
- For utility payments and transfers over **$200**, require Singpass and facial
  recognition.
- For stock transactions and other finance-related actions, require Singpass
  and facial recognition at every amount.

**Open boundary:** decide which method applies to transactions of exactly
**$200**.

**Reminder:** my friend is working on voice recognition.

**Test ideas:**

- “I want to send my friend John 5 dollars for dinner” — friend transfer under
  $200; verify voice recognition is used.
- “Transfer $20 to this PayNow for flowers: 81234567” — PayNow transfer under
  $200; verify voice recognition is used.
- “I wantttttt to invest some       money into Broadcom so hhhhhelp me
  allocate one thousand dollars into Broadcom” — noisy, disfluent stock
  investment request; verify Singpass and facial recognition are required.

## Pay tab contacts

**Status: Done**

Add a contact list to the Pay tab, including example contacts John Smith,
John Tan, and Alicia Powell.

## Activity

Create an Activity page that tracks purchased stocks, paid bills, and transfers
to friends. Show real changes as transactions occur, with entries reflecting
their actual status and outcome.

## Bills and Stocks pages

**Bills status: Done**
**Stocks page (DigiWealth) status: Done**

Create separate Bills and Stocks pages for their respective payment and
investment actions.

## Combined multi-action request

Allow one user request to include multiple action types and handle them as one
intent, rather than requiring separate requests. For example: “Pay my water
bill, put $200 into Nvidia stock at market open, and pay my friend back for
dinner.” Resolve and present all three actions together for processing.

## Shared inference gateway

**Status: Complete**

Create a backend endpoint protected by a unified secret key (to be defined
later). Configure the project so a friend can clone the branch and send Qwen,
Nemotron voice, and Kokoro requests through this endpoint. The gateway will
relay each request to the corresponding service on my system, including the
GPU LLM supervisor. Retiring the endpoint URL will revoke access to these
hosted services.

**Follow-up checks:** add and verify a `.gitignore`; check the branch for
committed environment files and remove them from the branch if present.
