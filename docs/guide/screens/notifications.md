# Notifications

**Assumes:** you are signed in as an Owner or Super Administrator, the roles that can manage notifications, and are using the installed v4 release.

Notifications (`/notifications`) configures alert channels and decides which alerts use each channel. The page also
shows recent delivery results and open delivery incidents.

## What you can do here

- Add and edit email, Telegram, Slack, Microsoft Teams, or generic webhook channels.
- Send a test message, enable or disable a channel, and review its latest delivery result.
- Choose channels for critical incidents, tickets needing a person, reviews, daily summaries, and weekly reports.
- Set quiet hours in UTC. Critical incident alerts still send during quiet hours.

Saved credentials stay hidden. When changing a saved channel, leave a credential field blank to keep its current value.
Failed sends retry up to three times after the first attempt. Four failed attempts open a critical incident. Daily
summaries go out after 00:05 UTC; weekly reports go out on Monday after 00:05 UTC. Critical alerts are sent during
quiet hours.
