# Security

Greenlight drives coding agents, often with their permission prompts bypassed, and runs `glab`/`gh` with your credentials. Bugs here can matter.

**Report a vulnerability privately** through GitHub's [private vulnerability reporting](https://github.com/cbarraford/greenlight/security/advisories/new). Please don't open a public issue for it. You'll get a reply within a week.

In scope: anything that lets a repo, MR/PR, issue, comment or web page make Greenlight or its agents run commands, leak credentials or private data, or escape the dashboard's Content-Security-Policy. Also anything that gets private data into the diagnostic trail (`scripts/diag.py`), which is built to be shareable.
