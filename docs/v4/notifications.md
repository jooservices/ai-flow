# Notifications

Administrators can open **Notifications** in the v4 console to configure email, Telegram, Slack, Microsoft Teams,
and generic HTTP webhook channels. Email uses an SMTP endpoint, including hosted senders that provide SMTP access.
The registry stores channel settings and credentials; API responses expose public connection details and whether a
credential is saved, never the saved credential or webhook URL. To rotate a credential, enter its replacement and
save the channel.

Use **Send test message** on a channel to send a channel specific test and inspect its last result. A failed send is
queued for up to three retries after the initial attempt. The Notifications screen shows pending and completed
deliveries; four failed attempts open a critical incident there. A later successful retry closes that incident.

The routing table selects channels independently for critical incidents, tickets needing a person, review before
merge, daily summaries, and weekly reports. Quiet hours are UTC and defer ordinary alerts until the end of the
configured window. Critical incidents bypass quiet hours. The control loop sends the previous UTC day's summary
after 00:05 UTC and the previous Monday through Sunday report on Monday after 00:05 UTC. Summaries include finished
tickets, work waiting on a person, failed runs, and total recorded run spend.

Messages identify the project, ticket or system event, requested action, and a direct action link. Ticket decisions
link to Jira; review alerts link to Human review; incident alerts link to Command Center; reports link to the
relevant console view. Deployment scripts call the control plane's `aiflow4 notify` command, which uses the same
saved channels as the console.

The **owner** and **Super Administrator** console roles can manage notifications. Other roles cannot open the screen
or call its API.
