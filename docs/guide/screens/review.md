# Human review

**Assumes:** you are signed in as an Administrator, Product owner, Delivery, Team Lead or Reviewer, and you have the reviewer access token from your administrator.
The token is never shown in the documentation.

Human review (`/review`) lists the tickets that wait for a person before merge. It refreshes every 15 seconds.

## What you see

For each ticket: the pull request, the verify result, the facts needed to decide, and the available review actions.
The note field is used by Reject, Send for rework, Answer, and Ask AI a question; a note stays in the field if the
queue refreshes and clears after its action succeeds.

## Common tasks

1. Enter the reviewer access token. It is held only in the page until you close it.
2. Read the ticket and its pull request.
3. Choose **Approve and merge**, or write a note and choose **Reject**, **Send for rework**, **Answer**, or
   **Ask AI a question**.
