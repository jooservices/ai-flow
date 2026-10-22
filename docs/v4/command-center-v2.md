# Command Center V2 design prototype

`design/command-center-v2.html` is a single-file interactive prototype (plain HTML and JavaScript, sample data, no backend) of the
new Command Center. Open it in a browser. It is the visual and behavioural reference for the tickets that build the new console:

- Warm graphite dark theme; one colour per AI provider, always named in text; status written in words, never colour alone.
- One menu of role views. Dashboard and Work board first, open to every role; role views next; setup views last.
- Dashboard has two sections, Work and AI and machines. Project choice is a searchable picker with stars (built for hundreds of projects), never a row of buttons.
- Work board: pick a project, get its board (Backlog, Intake, To Do, In Progress, In Verify, Human review, Done).
- Every AI account is shown with quota used, its own reset time, tokens and cost, per AI and per machine.
- Machines and AI: add a machine with a one-time command, add AI accounts with a sign-in link, sessions at once, jobs (develop, verify, intake, master).
- Users and roles: people, the role to view matrix (drives "View as"), history of changes.
- Human review: metrics on how often a person is needed, and the actions approve, reject, send back, ask the AI a question, answer.
- Knowledge: what was learned, context pack for a new session, items saved automatically.
- Ctrl K command bar to jump to a view, a project or a ticket.

The numbers in the prototype are sample data. It is documentation: do not ship it as part of the product.
