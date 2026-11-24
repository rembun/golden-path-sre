# Runbook: <failure name>

<!-- One page. Written for whoever is on call at 3 a.m., who may not know this service.
     Copy to docs/runbooks/<failure>.md, fill in every section, link it from the alert's
     runbook_url (kit/slo spec field `runbook_url`), and delete these comments. -->

| | |
|---|---|
| **Service** | <service name> (namespace `<namespace>`) |
| **Alerts** | <alert names that link here, and which one pages> |
| **Owner** | <team>, <channel> |
| **Last reviewed** | <date>, by <name> |

## Customer impact
<!-- What customers see, in their words. Which journeys break. How fast the error budget burns. -->

## Check (first 5 minutes)
<!-- The 3-5 queries or commands that confirm what is happening and narrow the cause.
     Give the exact PromQL / kubectl, and what "bad" looks like for each. -->
1. Is the SLO burning? `<query>` (bad: …)
2. What is failing? `<query>` (bad: …)
3. Is it us or a dependency? `<query or command>`

## Mitigate (stop the impact first)
<!-- Ordered steps, safest first. Each step: the command, what it changes, how to tell it worked.
     Also say what NOT to do: the tempting action that makes this failure worse. -->
1. …
2. …

**Don't:** …

## Fix (remove the cause)
<!-- What the real fix is, who owns it, and where it gets tracked. Often a different team. -->

## Verify recovery
<!-- The signals that prove it's over: queries and the values they should return. -->

## Escalate and communicate
<!-- When to escalate and to whom. Where status updates go. -->

## Afterwards
<!-- Post-incident review if the page fired; error budget impact; follow-up tickets. -->
