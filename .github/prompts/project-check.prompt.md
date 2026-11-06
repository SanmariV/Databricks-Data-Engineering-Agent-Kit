---
agent: agent
description: Run the project health check (dbde check, plus the dev test results) and explain what to fix, in order.
---

# Project health check

Check this project's structure and data quality. Follow `AGENTS.md`. This check must be fast: run each command once, don't re-read files, and don't narrate each command.

## Steps

1. Offline rules and render drift:

   ```bash
   python -m dbde check
   ```

   Each line is `<severity> <rule> <file>: <message>`. Rule ids are explained in `databricks_engineering_standards.md`, section 12.

2. If a dev workspace is set up (`python -m dbde debug --target dev` succeeds), run the data quality tests once:

   ```bash
   python -m dbde test --target dev
   ```

   Skip this step if the user says the tests were just run, or if no dev workspace exists. Say which.

3. Group the findings, most serious first:
   1. rendered files out of date or hand-edited (R1) and specs that don't load (P0)
   2. layer rules (L*)
   3. missing keys, tests, descriptions and grain (S*, T*)
   4. failing error-severity data tests
   5. naming and types (N*, D*, G*)
   6. non-deterministic logic (I1)
   7. warnings: personal data to decide (P1), abbreviations (N8), bronze tables without silver (L7), warn tests

4. Do NOT fix anything yet.

## Chat summary (the only thing printed in chat)

```
Project check finished.

Result: <n> errors, <n> warnings from dbde check; data tests <n> pass, <n> fail, <n> warn (or "not run: <reason>").

Findings:
1. <rule in plain words> (<rule id>): <n> (<tables, up to 3>). Fix: <one line>.
2. ...

Your next steps:
1. <most important fix, with the file>
2. ...
(at most 5)

Reply "fix 1" to have me fix one item, or "explain 2" for details.
```

If everything passes, say so in one line. A rule that cannot apply yet (for example, no gold tables exist) is expected. Say why, and don't list it as a problem.
