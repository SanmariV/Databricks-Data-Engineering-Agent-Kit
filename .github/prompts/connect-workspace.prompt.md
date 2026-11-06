---
agent: agent
description: Connect the kit to a Databricks workspace. Asks for the project name, workspace URL and SQL warehouse names, writes dbde.yml, signs in and tests the connection.
---

# Connect to a Databricks workspace

- Project name: **${input:project:snake_case, e.g. colina_analytics}**
- Dev workspace URL: **${input:host:e.g. https://adb-1234567890123456.7.azuredatabricks.net}**
- Dev SQL warehouse name: **${input:warehouse:exact name from SQL → SQL Warehouses, e.g. Serverless Starter Warehouse}**
- Test and prod SQL warehouse names: **${input:release_warehouses:e.g. "test: DE Test, prod: DE Prod", or skip}**
- Catalog names: **${input:catalogs:default (dev_bronze, dev_silver, dev_gold, ...), or e.g. "dev: bronze_dev, silver_dev, gold_dev", or one catalog with bronze/silver/gold schemas, e.g. "dev: life_insurance"}**

Follow `AGENTS.md`. This prompt only changes `dbde.yml` and the rendered files. It never writes a host, token or id into the repo.

## 1. Ask for what is missing

If any input above is empty, ask for all the missing ones in **one** chat message, then wait for the reply. Use this message, and leave out the lines you already have:

```
To connect the kit I need:
1. Project name (snake_case), e.g. colina_analytics
2. Dev workspace URL: copy it from the browser address bar when you are in Databricks (up to .net or .com)
3. Dev SQL warehouse name: Databricks → SQL Warehouses, copy the name exactly
4. Test and prod SQL warehouse names, or "skip" if you don't know them yet
5. Catalog names, or "default" for dev_bronze / dev_silver / dev_gold.
   If you use ONE catalog with bronze, silver and gold schemas inside it
   (e.g. life_insurance.bronze, life_insurance.silver), give that catalog name.
```

- Never guess a value. If the user says "skip" for test and prod, keep `TODO(SQL warehouse name)` there and list it.
- If the user pastes a token, password or client secret, don't repeat it or write it anywhere. Tell them sign-in uses the browser instead, and recommend revoking the pasted token.

## 2. Write `dbde.yml`

- `project:` the project name.
- `targets.dev.warehouse:` the dev warehouse name; `targets.test.warehouse` and `targets.prod.warehouse` if given.
- `targets.<env>.catalogs: {bronze: ..., silver: ..., gold: ...}` only if the user gave non-default names.
- One catalog for all layers (the user gave a single catalog name, or the catalog holds `bronze` / `silver` / `gold` schemas): set every layer of `targets.<env>.catalogs` to that catalog, **and** set `layer_schemas: {bronze: bronze, silver: silver, gold: gold}` (use the user's schema names). Without `layer_schemas` the kit creates one schema per spec folder (`<catalog>.policy_admin.Policy`) instead of `<catalog>.silver.Policy`. Check the schemas first: `databricks schemas list <catalog> -p dbde-dev`.
- Keep `profile: dbde-dev` (and `dbde-test`, `dbde-prod`). **Never write the workspace URL into `dbde.yml` or any other repo file.** It goes only into the CLI profile in step 3.

## 3. Sign in

1. Check the Databricks CLI: `databricks --version`. If it is missing, run `winget install Databricks.DatabricksCLI`. Then tell the user to reopen VS Code, and stop.
2. Check for an existing sign-in: `databricks current-user me --profile dbde-dev`. If it prints a user whose workspace matches the URL, skip to step 4.
3. Otherwise run:

   ```powershell
   databricks auth login --host <dev workspace URL> --profile dbde-dev
   ```

   Before you run it, tell the user: "A browser window opens. Sign in to Databricks there, then come back here." The command finishes when they are signed in. If it waits more than a few minutes, ask the user whether the browser tab opened.

## 4. Test and render

```powershell
python -m dbde render
python -m dbde debug --target dev
python -m dbde check
```

- `debug` must end with `debug: connection OK`.
- `SQL warehouse '<name>' not found`: show the user the names it lists, and ask which one to use (step 1).
- Missing catalogs: tell the user to create them or ask an admin (`START_HERE.md`, step 3), or to give the real names (step 1).
- Permission errors: tell the user what is missing (`START_HERE.md`, step 5).
- `check` may still warn (P2) for test and prod warehouses that were skipped. That is expected.

## Finish

End with the standard block from `AGENTS.md`, "How to finish every task". Under "Decisions for you", list every value left as `TODO`. If the connection works, make the first next step `/scaffold-databricks-project`.
