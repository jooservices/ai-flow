# Stop giant files growing

The **Giant files** pull-request check compares source files with the develop tree. On every pull request it prints
each baseline source file over 1,500 lines, including the control API, console page and any other oversized source
file. A pull request fails when one of those existing files gets longer. It also fails when a newly added text file
has more than 1,500 lines. Shortening an existing giant file passes, and the limit is exclusive: a new file at
exactly 1,500 lines passes.

The check uses the pull request's base commit when the target is `develop`; for pull requests targeting another
branch, it uses the current `origin/develop` tree. It counts regular UTF-8 text blobs from Git and ignores binary
files. The failure message tells the developer AI to put new feature code in a new module.

JAIF-253 hot-file scheduling covers every giant source file in the develop baseline, the v4 node agent, the shared
v4 test fixtures and suites. Tickets predicted to touch any of those paths wait while another ticket has a running
development run or an open pull request, so the guard does not encourage parallel work on the same files.

The guard is implemented with Python's standard library and Git, and runs as a separate required pull-request CI
job. Its policy is covered by `tests/v4/test_giant_files.py`; scheduling coverage is checked alongside the other
merge-conflict tests.
