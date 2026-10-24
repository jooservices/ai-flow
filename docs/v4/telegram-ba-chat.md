# Telegram brainstorm with the BA AI

"Owner nhắn trên Telegram; BA AI hỏi lại, tóm tắt, đề xuất epic và story; owner duyệt trong chat; AI tạo ticket đúng template trên Jira. Sau đó là Slack và Teams."

## Set it up

An owner or Super Administrator opens **Notifications** in the v4 console, adds or edits a Telegram channel, enters the bot token and the owner's private chat ID, selects a configured Jira project, and enables **BA brainstorming chat**. The token is write-only in the screen and is never included in API responses. The existing notification channel and Jira connection are reused. Developers can also run `aiflow4 ba-chat setup --name "Owner brainstorm" --chat-id ID --project KEY`; the CLI reads the token through a hidden prompt, or from stdin with `--token-stdin`.

Use a private one-to-one Telegram chat with the configured bot and chat ID. The receiver ignores group chats, other chat IDs, other senders, and messages without text. Enabling the feature makes the control loop poll Telegram updates and workers process durable chat turns with the shared BA prompt in `prompts/v4/ba-chat.md`.

## Talk through a proposal

Send the request in Telegram. When important scope is unclear, the BA AI asks questions and waits for the owner's answer. Once it has enough information, it posts a summary followed by epics, stories, and any useful tasks. Every proposal item has a title and short description.

The owner can then:

- Send `/approve` to create the current proposal in Jira.
- Send `/reject` to discard it. No Jira tickets are created.
- Send any other message to ask for a change. The BA AI uses the previous proposal and conversation to post a revision; that revision still needs approval.

The conversation service stores drafts and messages, and queues delivery replies so transient Telegram errors can be retried. A worker may ask questions or return a draft, but it has no Jira write path. The control plane starts Jira creation only after an explicit owner approval while the conversation is in the proposal state. A later message after completion or rejection starts a new brainstorm.

## Jira tickets and failures

Approval passes the full proposal to one control-plane function. It creates an Epic, then its Stories with Jira's parent field set to the Epic key. Tasks under a Story become Jira Sub-task issues with that Story as parent. Every description uses the AI Flow template:

```text
Business requirement:
...

Acceptance criteria:
- AC1: ...

Technical notes:
...

Definition of done:
- ...
```

After the write, Telegram receives the created ticket keys and Jira links. If Jira refuses an item, the reply names that item and the short failure reason. Independent epics and their children continue; children of an epic or story that failed are skipped because their parent does not exist.

The Jira site and project come from the connected Jira settings and the project selected for the Telegram channel. The project must exist in the configured project list. No item is written while a proposal is being discussed, revised, or rejected.

## Extending to other chat channels

Conversation state, proposal validation, approval, and Jira creation are independent of Telegram. The Telegram adapter only polls and sends messages; Slack and Teams can be added as adapters in later work without changing the BA proposal or Jira creation functions.
