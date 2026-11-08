# Gemini review

Gemini Code Assist is a second AI reviewer on pull requests. It is free and installed on the organization, and it reviews pull requests that change the v4 code (`aiflow/v4`, `tests/v4`, `docs/v4`, `deploy/v4`, `prompts/v4`) against the rules in `.gemini/styleguide.md`; a change to the old flow only is not reviewed against those rules.

`.gemini/styleguide.md` lists the v4 rules of this repository, and each rule names the document it comes from. `.gemini/config.yaml` keeps only medium and higher comments, at most ten comments per pull request, a short summary, and no automatic help message when a pull request opens (`/gemini help` still lists the commands). `.coderabbit.yaml` is separate and configures CodeRabbit only.
