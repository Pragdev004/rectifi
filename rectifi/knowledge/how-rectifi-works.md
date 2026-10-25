# How Rectifi works

Rectifi is a personal expense tracker that runs on your own laptop. Your data stays in a Postgres database on your machine.

## Where entries come from
- You type them in Add, or tell the assistant ("spent 250 on Swiggy").
- You paste a bank SMS.
- You import a bank statement (CSV, Excel or PDF).
- Optionally, your phone forwards bank SMS to your laptop through a private connection.

## Why you can trust the numbers
Every amount is calculated by plain code and SQL, never by an AI model. The assistant can read your numbers and call tools, but a model is never allowed to decide an amount. If an answer contains a rupee figure Rectifi cannot trace to your records or to what you typed, it is flagged or held back.

Duplicates are blocked by the database itself. Imports are checked against your balance. If the check does not add up, Rectifi shows the gap instead of claiming it is complete.

## Privacy
Account, card, phone and UPI reference numbers are masked before text goes to any AI model. By default the model runs on your laptop through Ollama. Web search is off unless you switch it on and agree each time.

## The Library
The Library holds these guides and your own notes. The assistant can look things up there, check what it found, and show which sources it used. The guides contain no rupee figures, so amounts only ever come from your own records.

## Your control
You can undo an import, delete library files and see how each answer was checked.
