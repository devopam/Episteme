# Security policy

## Supported versions

Episteme is developed on the `main` branch only. Security fixes land on `main`; there are no maintained release branches.

## Reporting a vulnerability

Please report security problems privately first, so they can be fixed before details are public:

- **Email:** devopam@gmail.com, with "Episteme security" in the subject line, or
- **GitHub private report:** the repository's **Security** tab → **Report a vulnerability** (GitHub private vulnerability reporting).

Please include:

- what is affected (file, script, pipeline stage or dependency) and the commit you tested;
- how to reproduce it, and what an attacker could do with it;
- any fix or mitigation you suggest.

**Do not put exploit details, credentials or other secrets in a public issue.** Once a problem is fixed, or if it is not sensitive (for example a hardening suggestion or an outdated dependency with no known exploit here), log a [GitHub issue](https://github.com/devopam/Episteme/issues) so it is tracked in the open.

## What to expect

Reports are handled on a best-effort basis by the maintainer. You will get an acknowledgement, a decision on whether the report is accepted, and, for accepted reports, a note when the fix is on `main`. Credit is given in the fix or advisory unless you ask otherwise.

## Scope

In scope: the code, scripts and configuration in this repository, and how it handles credentials and downloaded data.

Out of scope: vulnerabilities in the upstream data sources or third-party services the pipeline downloads from (report those to their owners), and issues in dependencies that are already tracked by a Dependabot alert here.
