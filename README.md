# Rectifi

A personal expense tracker that runs on your own laptop.

You tell it what you spent. Or you paste a bank SMS. Or you import a bank statement. Rectifi stores it, sorts it into categories, and shows you where your money goes. Then it does something most trackers skip: it **proves nothing is missing**, using the bank's own balances.

It has two ways in:

- the **app** (a web app; the old Streamlit app is still there as `--streamlit`): Overview, Review, Insights, Ask, Add, Ledger, Budgets, Accounts, Phone
- a **read-only MCP server**, so Claude Desktop can answer questions about your money (it can look, never change)

## The five rules

1. **The AI never decides a number.** Any amount it wants to save must appear in what you typed. If it doesn't, Rectifi refuses.
2. **Money is never a float.** It is `Decimal` in Python and `NUMERIC` in Postgres. No rounding crumbs.
3. **Duplicates are blocked by the database.** Not by app code. Paste the same SMS twice and you get one row.
4. **Completeness is proven.** `opening balance + credits - debits = closing balance`. If that is off by even one rupee, you see the gap.
5. **Everything is local.** The data lives in a Postgres on your laptop. Account numbers are masked before anything goes to an AI model.

## What leaves your laptop

| What you use | What leaves |
|---|---|
| Ollama (default) | Nothing. |
| OpenRouter (`LLM_PROVIDER=openrouter`) | Your chat messages and the results the agent reads go to OpenRouter, and from there to whoever hosts the model. Free models are hosted by third parties, and some may keep or train on what you send. Check the model's page on openrouter.ai. |
| Hugging Face (`LLM_PROVIDER=huggingface`) | The same, to Hugging Face and the inference provider it routes to. |
| Any other OpenAI-compatible service (`openai_compat`, for example Groq) | The same, to whoever runs that URL. |
| DeepSeek (`LLM_PROVIDER=deepseek`) | The same, to DeepSeek. |
| Claude Desktop with the MCP server | The results of the questions Claude asks go to Anthropic. Same masking. |
| Always | Your statement files (CSV, Excel and PDF), the raw SMS text, and the database stay on your laptop. PDFs and phone SMS are read by fixed rules; no model ever sees them. |

For every cloud model, long account, card and reference numbers are masked before anything is sent. That happens in one place (the agent), so it is the same for every provider. API keys stay in your `.env` and are never printed.

Two honest limits:

- Masking hides long digit runs (account, card, UPI reference, phone). It does **not** hide names or shop names. A payee like "Ravi Kumar" can reach the model.
- The app's font files load from Google Fonts, so your browser asks Google for fonts. None of your data is sent.

## What you need

- **Python 3.13** (python.org). 3.11 and 3.12 should work too, but 3.13 is what it was tested on.
- **PostgreSQL 16**: either your own (Windows: the EDB installer) or Docker Desktop
- Optional, for the Ask page: a model that supports **tool calling**. See "Choose a model" below.

## Windows quick start (PostgreSQL already installed)

If you installed PostgreSQL 16 with the EDB installer, one command does the rest. It needs no admin rights and no Docker. Open PowerShell in the Rectifi folder and run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
```

(`-ExecutionPolicy Bypass` only applies to this one run. Your PowerShell settings are not changed.)

It will:

1. find `psql.exe` and check the PostgreSQL service is running
2. ask for the password of the `postgres` user (the one you chose when you installed PostgreSQL; typing is hidden) and create the `rectifi` login and the three databases
3. make `.venv` and install the packages (Python 3.13, 3.12 or 3.11, found through the `py` launcher)
4. create `.env` (a value you set yourself is never overwritten)
5. help you pick a model for the Ask page, or skip it
6. create the tables and run `doctor`

Then it prints the command to start the app:

```powershell
.venv\Scripts\python.exe scripts\dev.py up
```

and you open http://localhost:8501. You can run the setup again at any time. Options:

| Option | Meaning |
|---|---|
| `-Port 5433` | Your PostgreSQL is not on the default port 5432. |
| `-Llm ollama` / `openrouter` / `huggingface` / `skip` | Choose the Ask page model without being asked. |

If the PostgreSQL service is stopped and Windows says you need admin rights, the script prints the one command to run in an administrator PowerShell (`Start-Service -Name 'postgresql-x64-16'`), then run the setup again.

> The script was written for Windows PowerShell 5.1 and checked on Linux with PowerShell 7. It has not been run on a real Windows PC yet, so if something looks wrong, the manual steps below still work.

## Install

### Windows (PowerShell)

```powershell
git clone <your-repo-url> rectifi
cd rectifi
python scripts/dev.py setup
```

If `python` is not found, try `py -3.13 scripts/dev.py setup`.

### macOS (Terminal)

```bash
git clone <your-repo-url> rectifi
cd rectifi
python3 scripts/dev.py setup
```

`setup` makes a `.venv` folder, installs the exact package versions from `requirements.txt`, and creates your `.env` file. You do not need to "activate" anything. The helper script finds `.venv` by itself.

From here on, type `python` on Windows and `python3` on macOS.

## The database

### Path A: Docker (easiest)

Open Docker Desktop. Wait until it says it is running. Then:

```
python scripts/dev.py db
```

This starts Postgres 16 in a container. It creates your database `rectifi`, plus `rectifi_test` (for the tests) and `rectifi_demo` (for the demo). The port is open to your laptop only, not to your Wi-Fi.

### Path B: your own Postgres 16

Use this if you do not want Docker.

**Windows:** install Postgres 16 from postgresql.org (the installer from EDB). Remember the `postgres` password you set. Then run the [Windows quick start](#windows-quick-start-postgresql-already-installed) above. Doing it by hand instead:

```powershell
psql -U postgres -f db\init.sql
```

**macOS:**

```bash
brew install postgresql@16
brew services start postgresql@16
psql postgres -f db/init.sql
```

`db/init.sql` creates the login `rectifi` and the three databases. It is safe to run again.

If your Postgres is somewhere else, change `DATABASE_URL` in `.env`.

## Choose a model for the Ask page

The Ask page is optional. Everything else works without it. The agent **needs a model that supports tool calling** (a model that can only chat will not work).

| | Cost | Limits | Your data | Good for |
|---|---|---|---|---|
| **Ollama** | Free | Needs RAM: about 8 GB free for the 7B model | Never leaves your laptop | The private, no-account choice |
| **OpenRouter `:free` models** | Free | About 20 requests a minute and **50 a day** (1,000 a day after you buy at least 10 credits). The list of free models changes, and only some support tools. | Goes to OpenRouter and the model host | A laptop that cannot run Ollama |
| **Hugging Face** | **Not free on a free account.** Free accounts get $0 of credit. PRO gives $2 a month. | Your credit | Goes to Hugging Face and the provider it routes to | Only if you already pay |
| **DeepSeek** | Paid, cheap | Your credit | Goes to DeepSeek | Cheap, pay as you go |
| **Other OpenAI-compatible** (`openai_compat`, for example Groq) | Depends on the service | Depends | Goes to that service | Anything not listed |

### Ollama (free, private)

Install it from ollama.com, then:

```
ollama pull qwen2.5:7b
```

Nothing to change in `.env`, Ollama is the default. On a laptop with little RAM use `ollama pull qwen2.5:3b` and set `OLLAMA_MODEL=qwen2.5:3b` (it makes more mistakes with tools). These model families support tools: qwen2.5, qwen3, llama3.1, llama3.2, mistral-nemo.

### OpenRouter free models

1. Make a key at openrouter.ai/keys.
2. List the free models that support tools (this needs internet; it uses only Python's standard library):

   ```
   python scripts/dev.py free-models
   ```

3. Put one in `.env`:

   ```
   LLM_PROVIDER=openrouter
   OPENROUTER_API_KEY=your-key-here
   OPENROUTER_MODEL=<an id from the list, ending in :free>
   ```

There is no default model on purpose: the free list changes, and a model that worked last month may be gone. If a model stops working, run `free-models` again and pick another. If you hit the daily limit the Ask page says so.

### Hugging Face

Not free on a free account: free accounts get $0 of Inference Providers credit, so every question fails with HTTP 402. It works if you have PRO ($2 of credit a month) or bought credits. Make a fine-grained token with "Make calls to Inference Providers" at huggingface.co/settings/tokens, then:

```
LLM_PROVIDER=huggingface
HF_TOKEN=your-token-here
HF_MODEL=deepseek-ai/DeepSeek-V3.1:cheapest
```

`HF_MODEL` is optional (that is the default). The part after the colon picks the provider: `:cheapest`, `:fastest`, or a provider name.

### DeepSeek

Get a key from platform.deepseek.com. In `.env`:

```
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=your-key-here
```

### Any other OpenAI-compatible service (Groq and others)

```
LLM_PROVIDER=openai_compat
OPENAI_COMPAT_BASE_URL=https://api.groq.com/openai/v1
OPENAI_COMPAT_API_KEY=your-key-here
OPENAI_COMPAT_MODEL=llama-3.3-70b-versatile
```

Never commit `.env`. It is already in `.gitignore`.

If the model is not ready, the Ask page tells you exactly what to fix and the rest of Rectifi keeps working. Every cloud model uses a 60 second timeout and two retries, and temperature 0.

## Run it

```
python scripts/dev.py doctor
python scripts/dev.py up
```

`doctor` checks Python, packages, the database and the model, and tells you what to fix. `up` starts the database, creates the tables, and opens the app at http://localhost:8501.

`up` skips Docker if your own Postgres is already running, so it works on Path B too. (On Windows after the quick start, use `.venv\Scripts\python.exe scripts\dev.py up`.)

## The web app

Rectifi's screen is a web app (React, built once and committed in `web/dist/`), served together with its small JSON API from one Python server on http://localhost:8501. **Running it needs only Python and Postgres.** There is nothing to install for the screen and no Node.

It has ten pages: Overview, Review, Insights, Ask, Add, Ledger, Budgets, Accounts and Phone. On a phone it has a bottom bar (Home, Ledger, Add, Ask, More); on a laptop a sidebar. Light and dark follow your device, with a switch in the sidebar (or under More).

It only shows what Rectifi's Python code worked out. Money crosses the API as text like `"1250.50"` and the browser only formats it. The same rules apply as before: amounts the assistant saves must be ones you typed, every number is worked out by `rectifi/tools.py`, and Postgres is the final guard against double entries.

**Fallback: `--streamlit`.** The old Streamlit app still works, unchanged:

```
python scripts/dev.py up --streamlit
python scripts/dev.py demo --streamlit
```

**Rebuilding the web app needs Node; running it doesn't.** You only need Node 20.19 or newer if you change something under `web/`:

```
python scripts/dev.py build-web    # npm ci, type check, tests, then web/dist
python scripts/dev.py web          # develop: the API with reload on :8501 and Vite on http://127.0.0.1:5173
```

Commit the new `web/dist/` with the change. `python scripts/dev.py doctor` tells you if `web/dist` is missing or older than the sources.

Want to look around first with fake data? Run `python scripts/dev.py demo`. It uses a separate database (`rectifi_demo`) on http://localhost:8502. Your real data is never touched.

## Your first 10 minutes

1. **Accounts:** add your bank account (for example "SBI Savings", last 4 digits `1234`).
2. **Accounts:** record today's balance from your banking app. Later, record another one. Two balances on two different dates are what make the completeness check possible.
3. **Add > Quick entry:** type an expense you made today.
4. **Add > Paste bank SMS:** paste a real bank SMS. Rectifi reads the amount, date, payee and UPI reference.
5. **Budgets:** set limits for the 3 or 4 categories where your money goes.
6. **Ask:** type `spent 250 on swiggy`, then `how much did I spend on food this month?`
7. **Overview:** look at your savings rate and the completeness check.
8. **Review:** see your first weekly review (more below).

## Importing a bank statement

1. Download a **CSV**, **.xlsx** or **PDF** statement from net banking. Old `.xls` files are not supported: open them in Excel and Save As `.xlsx` or CSV.
2. Go to **Add > Import statement** and upload it. That is all. When Rectifi is sure it understood the file, it **imports it straight away**: there is no Import button to press.
3. Read the result card. It says how many rows were **added**, **merged** with something you typed by hand, **already recorded** (skipped) and **need review** (not imported, with the reason for each), which account they went into and why, and the balance check.
4. Wrong? Press **Undo this import** on the card. It removes exactly the rows that import added and puts the balance records back the way they were. Rows that were only merged or skipped are not touched.

When does it import by itself? Only if all of these are true: it found the date, the description and the amount columns (or money-in and money-out columns), nearly every row could be read, and it knows which account the rows belong to. Otherwise it falls back to the **preview**: it shows the columns it found, you fix them, pick the account and press Import, like before. To always check first, tick **Let me check the columns and choose the account myself** above the upload box.

Which account? If the statement says the account's last 4 digits and one of your accounts has them, that account. Otherwise your default account (the only one, or the one you used last). A statement whose digits match none of your accounts is **not** imported by guess: you choose.

What happens to the rows:

- Rows you already have (same UPI reference, or the same row imported before) are skipped.
- Entries you typed by hand are **merged** with the matching bank row (same amount, same direction, within 2 days). You do not get double entries.
- Two identical rows on the same day (two ₹20 chai payments) both import. Importing the file again changes nothing, and Streamlit re-running the page does not import it a second time.
- A row that could not be read, or a PDF row whose money-in or money-out could not be proven, is **never imported by guess**. It is listed on the card, and the balance check shows the gap until you add it by hand.
- The first and last balances in the file become your opening and closing balances, and the completeness check runs straight away.

Import each period **once**, in one format. A CSV and a PDF of the same month spell some narrations slightly differently. UPI payments are still recognised by their reference, but a card or NEFT row could be counted twice, and the balance check would then show a gap.

## Import a PDF statement

Most banks let you download a PDF. Rectifi reads it on your laptop with `pdfplumber`. It reads the PDF's own text, so there is no OCR and no AI. The PDF never leaves your laptop.

1. **Add > Import statement**, then upload the `.pdf`.
2. If the PDF is protected, Rectifi says so. Type the password in the **PDF password** box and press **Read the PDF with this password**. If your bank's email gave a password for this PDF, enter it. Rectifi does not guess what it looks like. The password is used once to open the file. It is not saved, not logged, and the box empties itself.
3. From here it is the same as a CSV: check the columns, pick the account, import. Duplicates are skipped, hand entries are merged, and the balance column proves nothing is missing.

How it reads the PDF:

- **Tables first.** Every table on every page is joined into one. A header repeated on each page is dropped. Blank rows are dropped. A narration wrapped onto a second line (inside the cell, or on a row of its own) is joined back to its row. A summary box at the end is ignored.
- **Plain text second.** If the PDF has no table lines, Rectifi reads lines that start with a date and end with an amount and a balance. It works out **money in or out from how the balance moved**. With no balance on a row it uses a Dr/Cr mark next to the amount. If neither settles it, the row is marked **needs review** and is **not imported**. Rectifi never guesses. The balance check shows the missing row as a gap until you add it by hand.

What it cannot do:

- A **scan or a photo** has no text, so Rectifi says it can't read it. Download the CSV or Excel statement, or a PDF where you can select text.
- A PDF with no transactions in it says so.
- Odd layouts exist. If a PDF imports the wrong rows, check the preview before you click Import, or use the CSV.

## Use Rectifi on your phone

Rectifi runs on your laptop and listens on **127.0.0.1**, which means "this laptop only". It does not open itself to the Wi-Fi. That matters on a hostel network: your money is not on offer to everyone who is connected. To use it from your phone (and to let your phone forward bank SMS) you use **Tailscale Serve**. Tailscale is a free private network between your own devices. Serve is a small HTTPS proxy on your laptop that only devices signed in to *your* Tailscale account can reach.

One-time setup:

1. Install **Tailscale** on the laptop and on the phone (tailscale.com/download). Sign in to the **same account** on both. Keep it switched on.
2. In the Tailscale admin console (login.tailscale.com), open the **DNS** page and switch on **HTTPS Certificates**. Tailscale needs this once to give your laptop an `https://` address.
3. Start Rectifi: `python scripts/dev.py up`
4. In a second terminal: `python scripts/dev.py tailscale`

Step 4 runs these two commands (it prints them first so you can see them) and then prints your addresses:

```
tailscale serve --bg --https=443  http://127.0.0.1:8501     # the app
tailscale serve --bg --https=8443 http://127.0.0.1:8765     # the SMS gateway (only if RECTIFI_SMS_GATEWAY=1)
```

Then, on the phone (Tailscale on), open `https://<laptop-name>.<tailnet>.ts.net/`. The **Phone** page in Rectifi shows the exact addresses and has an **Open Rectifi on your phone** card. Add it to the home screen from the browser menu and it behaves like an app.

Three settings guard the web app (all optional, in `.env`):

- `RECTIFI_TAILSCALE_USER=you@example.com` locks the app to one Tailscale login. Tailscale Serve adds a `Tailscale-User-Login` header; a request from any other login (a device shared to someone else, a tagged device) gets 403. A browser on the laptop itself is still let in. Without this line, any device on your own tailnet can open the app.
- `RECTIFI_ALLOWED_HOSTS` is the list of names the app answers to (default `localhost,127.0.0.1,::1,*.ts.net`). A request for any other name is refused, which stops a web page from reaching your laptop through DNS tricks.
- `RECTIFI_ALLOWED_ORIGINS` adds extra web addresses allowed to make changes, for example a custom domain in front of Tailscale. You normally leave it empty.

Whether Tailscale Serve passes the browser's own name through is not confirmed on a real install (see PROGRESS.md, Not verified). If the page says "This address isn't allowed", add the name it shows in your address bar to `RECTIFI_ALLOWED_HOSTS`.

Other commands:

- `python scripts/dev.py tailscale status` shows the addresses and whether they are published.
- `python scripts/dev.py tailscale off` stops publishing. If that fails, `tailscale serve reset` clears every Serve rule on the laptop (also ones that are not Rectifi's).
- `python scripts/dev.py doctor` says whether Tailscale is installed and which address Rectifi would use.

Things to know:

- **Nothing is on the open Wi-Fi.** The app and the gateway bind to `127.0.0.1`. Check with `python scripts/dev.py doctor`.
- **Your laptop's name is public.** Tailscale's HTTPS certificates are written to a public log (Certificate Transparency), so the name `<laptop>.<tailnet>.ts.net` becomes visible to anyone who looks. The pages behind it stay private. If the laptop name is personal ("praggu-hostel-pc"), rename the device in the Tailscale admin console before step 4.
- **The laptop must be on** and Rectifi running. Otherwise the phone has nothing to talk to.
- **Streamlit's protections stay on.** `.streamlit/config.toml` keeps `enableXsrfProtection` and `enableCORS` on, and lets only `localhost`, `127.0.0.1` and `*.ts.net` host names connect. If Tailscale is running when you start `dev.py up`, your exact `https://<laptop>.<tailnet>.ts.net` is also added to Streamlit's allowed origins, which covers a proxy that changes the Host header. If you put your own domain in front, add it to `allowedHosts` in that file.
- **Advanced, not recommended: `RECTIFI_SMS_HOST=0.0.0.0`.** This makes the SMS gateway listen on the whole local network, for the case where you use the same Wi-Fi with no client isolation and do not want Tailscale. Anyone on that network can then reach the gateway (the token is the only lock) and there is no HTTPS. Rectifi prints a warning and the Phone page shows the LAN address only in this mode. Prefer Tailscale.

Not verified (see PROGRESS.md): the Tailscale commands above follow Tailscale's documentation and were never run against a real Tailscale install, on Windows or otherwise. If a command fails, the script prints the message and what to try.

## Why there's no "link by phone number"

People ask for "enter my phone number and Rectifi reads my bank SMS". It cannot work, for plain reasons:

- A website or a laptop program **cannot read your SMS**. Phones keep messages in the phone. Only an app on the phone, which you allow to read SMS, can see them.
- A phone number is only an address. Knowing it lets someone **send** to you, not read what you received.
- A feature that did claim to do this would be a company reading your bank messages, or an app that wants you to forward them anyway. Rectifi is local-first on purpose.

So Rectifi does the honest version: an app **on your phone** (SMS to URL Forwarder) forwards each bank SMS to the gateway on your laptop, with a secret token you made on the Phone page. If the laptop was off, anything missed is caught when you import the statement and the balance check shows the gap.

## Auto-capture bank SMS from your phone

**The honest part first.** Nothing can read SMS "from a phone number". A laptop cannot see your messages. What works is the other direction: an app on your **phone forwards** each bank SMS to your **laptop**, over your own network. Rectifi then reads it with fixed rules (no AI), adds it, and skips duplicates.

What this needs:

- the laptop is **on** and Rectifi is **running**
- the phone and the laptop can reach each other, through Tailscale (see "Use Rectifi on your phone")
- the gateway is **switched on**. It is off by default.

If the laptop is off, the Android app keeps retrying for a while. Anything still missed is caught later by importing the statement, and the balance check shows the gap. Nothing is lost silently.

### 1. Turn the gateway on

Put this in `.env` (copy it from `.env.example`), then start Rectifi:

```
RECTIFI_SMS_GATEWAY=1
RECTIFI_SMS_PORT=8765
```

```
python scripts/dev.py up
```

`up` now starts the gateway too, and stops it when you press Ctrl+C. To run only the gateway, use `python scripts/dev.py phone`. `python scripts/dev.py doctor` says whether it is on.

The gateway listens on `127.0.0.1` only. Run `python scripts/dev.py tailscale` so your phone can reach it over HTTPS (see "Use Rectifi on your phone"). Windows Firewall should not ask anything, because nothing listens on the network. (It will ask only if you chose `RECTIFI_SMS_HOST=0.0.0.0`. Allow Private networks only.)

### 2. Pair a phone (Android)

Open the **Phone** page. It shows whether the gateway is on, and walks you through the rest. In short:

1. Type a name for the phone and press **Create a pairing token**. The token is shown **once**. Rectifi keeps only a hash of it.
2. Install **SMS to URL Forwarder** (open source) from F-Droid or IzzyOnDroid. Its package name is `tech.bogomolov.incomingsmsgateway`. Allow SMS access, and set the app to "Unrestricted" in Android's battery settings.
3. In the app, add a rule with the values from the Phone page:

   - **Sender filter** (regex): `(?i)^[A-Z]{2}-[A-Z0-9]+`. Indian bank SMS come from IDs like `AX-HDFCBK` or `VM-SBIUPI`. This may need adjusting for your bank.
   - **Text filter** (regex): `(?i)(debited|credited|spent|received|withdrawn|Rs\.?|INR)`
   - **URL**: `https://<laptop-name>.<tailnet>.ts.net:8443/sms` (the Phone page shows your exact address first; `python scripts/dev.py tailscale` prints it too)
   - **Headers**: `Authorization: Bearer <token>`. The app's headers box takes JSON, like `{"Authorization": "Bearer <token>"}` (follow the hint in the app if it looks different)
   - **JSON body**: `{"from": "%from%", "text": "%text%", "sentStamp": %sentStamp%}`
   - If the app has a "Local network mode" option, tick it (it is for webhooks on your own network).

   The app can also sign requests with HMAC. Rectifi does not need that: the bearer token is the lock.
4. On the phone (Tailscale on), open `https://<laptop-name>.<tailnet>.ts.net:8443/health` in the browser. It should show `{"ok": true}`. If it does not, see the network notes below.
5. Back on the Phone page, press **Send a test message**. It proves the gateway and the token work.

The app retries when Rectifi answers with an error. Rectifi answers `200` for everything it understood (added, duplicate, ignored, needs review), so the phone does not keep re-sending those.

### What Rectifi does with each SMS

| SMS | Result |
|---|---|
| A clear debit or credit (`debited`, `credited`, `spent`, `received`, `withdrawn`, `sent` ...) | **added**, with source `sms`. The account is matched by the last 4 digits in the SMS, or it is the only account you have. |
| The same SMS again, or the same UPI reference | **duplicate**, nothing added |
| An OTP, an offer, a chat, anything that is not a transaction | **ignored**. Not stored at all. |
| Looks like a bank SMS but the fixed rules are not sure | **needs review**. Kept in the Phone page's "Needs review" list. **Add** opens the Add page filled in as far as the rules could read it; **Dismiss** throws it away. |

Two safety rules: an SMS from a plain phone number (a friend's chat) is ignored, and an SMS that mentions an OTP is never added automatically.

### Network notes

- **Hostel and campus Wi-Fi often blocks one device from reaching another** ("client isolation"). Tailscale goes around that, because both devices connect out to Tailscale and talk through that connection. It also works when the phone is on mobile data.
- If `/health` does not open on the phone: is Tailscale switched on **on the phone**? Is it the same account as on the laptop? Did `python scripts/dev.py tailscale` finish without an error? Is Rectifi running with `RECTIFI_SMS_GATEWAY=1`? `python scripts/dev.py tailscale status` shows what is published.
- Only if you chose `RECTIFI_SMS_HOST=0.0.0.0`: the Phone page also lists the laptop's LAN addresses (`ipconfig` shows the IPv4 Address on Windows), and the URL is `http://<laptop-ip>:8765/sms`. A laptop address on Wi-Fi can change.

### iPhone (untested)

> These steps are written from how Shortcuts works. Nobody has run them against Rectifi yet. Expect to adjust them.

1. Open **Shortcuts > Automation > + > Message**.
2. Set **Message Contains** to `debited`. Make a second automation for `credited`.
3. Choose **Run Immediately**.
4. Add the action **Get Contents of URL**: the URL from the Phone page, Method **POST**, a header `Authorization` = `Bearer <token>`, Request Body **JSON** with `text` = the message and `from` = its sender.

### Security

- The gateway listens on **127.0.0.1**, so nothing on your Wi-Fi can reach it. The phone reaches it through Tailscale Serve, which only your own signed-in devices can use. The device token is a second lock.
- Anyone who has the token **and** is on your tailnet can send the gateway fake SMS (which could add fake transactions to your books). Keep the token private. If a phone is lost, press **Revoke** on the Phone page.
- It is **off by default**. Turn it off again when you do not need it (set `RECTIFI_SMS_GATEWAY=0`, and `python scripts/dev.py tailscale off`).
- Tokens are long random strings. Only their SHA-256 hash is stored. The token is never logged or printed after you create it, and neither is any SMS text.
- `/health` needs no login and returns only `{"ok": true}`.
- Limits: 8 KB per message, 60 messages a minute per phone, and repeated wrong tokens are slowed down.
- Over Tailscale the traffic is encrypted twice (Tailscale's tunnel and HTTPS). With `RECTIFI_SMS_HOST=0.0.0.0` there is no HTTPS: on a shared network SMS text travels unencrypted. Do not do that on a network you do not trust.

## The weekly review

Open **Review** once a week. Sunday is a good day.

It shows, for Monday to Sunday:

- how much you spent, and how that compares with the same days last week
- where it went, and your three biggest payments
- anything that needs attention: entries still in "Other", budgets near or over their limit, accounts with a gap or no recent bank balance
- the commitment you made last week

Then you write **one commitment** for next week, like "no more than 3 delivery orders". Next week, Review shows it back to you. All the numbers come from the same deterministic code as everything else. The Ask page can read the review too (`how was my week?`).

## What the Insights page does and doesn't do

Open **Insights** to see what Rectifi noticed in your own numbers, ranked by how much it matters (**Act**, **Watch**, **Good to know**). The three most important ones also show on the Overview, and Review shows the **Act** ones.

It looks for:

1. **Data problems first**: entries with no account, no account at all, a big "Other" pile, an account not checked against the bank for 30 days, a balance gap. A wrong record makes every other number weaker, so these come first.
2. **Budget pace**: where this month is heading against each budget (not before day 5, because early days are too noisy).
3. **Spikes**: a category 30% (and at least ₹500) above your usual month, which is the average of the last 3 months that have data.
4. **Recurring payments**: the same payee at a similar amount about every month, with what it costs per month and per year, which ones are new and which went up in price.
5. **A large payment**: a recent one above three times your usual payment (and above ₹2,000).
6. **Savings rate**: a drop of 10 points or more, or a rise, over the last 3 full months.
7. **Spending with no budget**: a category you spend a lot in, with a suggested limit. Nothing changes until you press the button.
8. **Small leaks**: many small payments in one category that add up.

How to trust it:

- **Fixed rules and SQL, with `Decimal` money. No AI decides or computes anything on this page.** Every ₹ figure on a card comes from the numbers the rule computed, and a test checks that. It works with no model at all.
- **"Explain this"** is optional. It asks your model to put one insight in friendlier words. Only that insight's numbers, with account numbers masked, are sent. If the reply contains a ₹ figure that is not one of those numbers, Rectifi throws the reply away and shows the plain card instead.
- It **never changes your data by itself**. A button that does something (like setting a suggested budget) says what it will do, and you press it.

What it does **not** do:

- It is not financial advice. It says "your food spending is running ahead of its budget", never "invest in X".
- It does not guess. With under a few weeks of data, most rules stay quiet and the page tells you what is missing instead of inventing a trend.
- It cannot see what you did not record. If the laptop was off or you did not import a statement, the balance check and the data-quality cards say so.
- It does not know your reasons. A spike may be a trip or a gift. The page shows the number; you decide.
- Thresholds are fixed in `rectifi/insights.py` (all in one block at the top). They are rules of thumb, not tuned to you.

## Use it from Claude Desktop (MCP)

The MCP server is read-only. It cannot add, change or delete anything.

1. Run `python scripts/dev.py mcp-config`. It prints some JSON. It does not contain your password: the server reads the database address from your `.env`.
2. Open Claude Desktop's config file:
   - **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
   - **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
3. Paste the `"rectifi"` entry inside `"mcpServers"`.
4. Restart Claude Desktop.

Tools it gets: `spending_summary`, `budget_status`, `recent_transactions`, `list_accounts`, `weekly_review`, `reconciliation`, `insights`.

## Run the tests

```
python scripts/dev.py test
```

The tests need the `rectifi_test` database (made by `dev.py db` or `db/init.sql`). They **delete every table in it** on each test, so the tests refuse to run on a database whose name does not end in `_test`. Your real `rectifi` database is safe.

No API key and no Ollama are needed. The agent tests use a scripted fake model.

## Troubleshooting

| Problem | Fix |
|---|---|
| "Rectifi can't reach its database" | Start it: `python scripts/dev.py db`. Check `DATABASE_URL` in `.env`. |
| `setup_windows.ps1` says it cannot start the service | Starting a service needs admin rights. Run the command it prints in an administrator PowerShell, then run the setup again. |
| `setup_windows.ps1` says the password was not accepted | Use the `postgres` password you chose when installing PostgreSQL. You get 3 tries. |
| PostgreSQL is on a different port | `powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 -Port 5433` |
| "running scripts is disabled on this system" | Run it with `powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1`. |
| Ask page: HTTP 402 | Hugging Face with no credit. Free accounts have $0. Use Ollama or OpenRouter `:free`. |
| Ask page: HTTP 429 | OpenRouter free limit (20 a minute, 50 a day). Wait, or use Ollama. |
| Ask page: model does not support tools | Pick another model. For OpenRouter run `python scripts/dev.py free-models`. |
| `dev.py db` fails | Open Docker Desktop and wait for it to say "running". `docker compose logs db` shows why. |
| Port 5432 is already in use | You have another Postgres running. Stop it, or use it (Path B). |
| Ask page says the assistant is off | Read the message. Ollama not running, model not pulled (`ollama pull qwen2.5:7b`), or a missing key or model for the cloud provider you chose. |
| `python` not found (Windows) | Use `py -3.13` instead of `python`. |
| `python3` is the wrong version (macOS) | `brew install python@3.13`, then `python3.13 scripts/dev.py setup`. |
| Tests say "Can't reach the test database" | `python scripts/dev.py db`, or run `db/init.sql` once on your own Postgres. |
| Strange characters in files on Windows | The helper sets UTF-8 for you. Always start things with `python scripts/dev.py ...`. |
| PDF says "password-protected" | Type the password your bank gave for the PDF in the PDF password box. |
| PDF says it can't be read, scan or photo | It has no text. Download the CSV or Excel statement. |
| Phone page says the gateway is off | Add `RECTIFI_SMS_GATEWAY=1` to `.env` and run `python scripts/dev.py up`, or run `python scripts/dev.py phone`. |
| The phone can't open the app or `/health` | Is Tailscale on, on the phone, with the same account as the laptop? Run `python scripts/dev.py tailscale status`. See "Use Rectifi on your phone". |
| `dev.py tailscale` says HTTPS is not enabled | Open the Tailscale admin console, DNS page, switch on HTTPS Certificates, run it again. |
| `dev.py tailscale` says Tailscale is not installed | Install it from tailscale.com/download. On Windows it looks in `C:\Program Files\Tailscale` as well as on PATH. |
| The page loads on the phone but says it cannot connect | Streamlit refused the address. Start `dev.py up` again while Tailscale is running, or add your domain to `allowedHosts` in `.streamlit/config.toml`. |
| A statement did not import by itself | It was not sure of a column or the account, so it showed the preview. Fix the columns and pick the account. |
| I imported the wrong file | Press **Undo this import** on the result card. |
| The page says "The web app isn't built" | `web/dist` is missing. Restore it from the repository, or run `python scripts/dev.py build-web` (needs Node 20.19+), or use `python scripts/dev.py up --streamlit`. |
| "This address isn't allowed to use Rectifi" | The name in the address bar is not in `RECTIFI_ALLOWED_HOSTS`. Add it to `.env` and restart. |
| "Rectifi doesn't accept changes from that site" | The page was opened from an address the app does not know. Open it from http://localhost:8501 or your Tailscale address, or add the address to `RECTIFI_ALLOWED_ORIGINS`. |
| Not sure what is wrong | `python scripts/dev.py doctor` |

## What is where

```
web/
  src/               the web app's source (React, TypeScript, CSS Modules). Money is only formatted here, never added up
  dist/              the built app, committed so the laptop needs only Python (python scripts/dev.py build-web makes it)
  scripts/screenshots.py   development only: screenshots of every page at 390 and 1280 px, light and dark
app/
  streamlit_app.py   the old Streamlit pages (python scripts/dev.py up --streamlit)
  theme.py           colours and the small HTML pieces of the old app
rectifi/
  api/               the web app's server: a thin Starlette layer over tools.py, plus the guards (Host, CSRF header, body size)
  tools.py           the one place that does money maths (SQL + Decimal)
  importer.py        CSV/Excel/PDF statement import (auto-import check, import batches)
  insights.py        the Insights rules (fixed rules, no AI)
  tailscale.py       builds the Tailscale Serve commands and reads its status (standard library only)
  pdf_statement.py   reads PDF statements (tables, then text lines) with pdfplumber
  parsing.py         bank SMS and narration parsers (regex, no AI)
  sms_gateway.py     the small web server your phone posts bank SMS to (off by default)
  privacy.py         masks account/card/reference numbers
  money.py           Decimal helpers and rupee formatting
  db.py, schema.sql  Postgres connection and tables
  mcp_server.py      the read-only MCP server
  agent/
    graph.py         the LangGraph agent and its amount guard
    explain.py       optional "Explain this" for an insight, with a check on every figure
    tools.py         what the agent is allowed to call
    llm.py           picks the model: Ollama, DeepSeek, OpenRouter, Hugging Face or any OpenAI-compatible service
db/init.sql          creates the login and the three databases
scripts/
  dev.py             setup, db, doctor, app, up, build-web, web, phone, tailscale, demo, free-models, test, mcp-config
  setup_windows.ps1  one-command Windows setup for an existing PostgreSQL
  seed_demo.py       fake data (only into a database ending in _demo)
tests/               pytest, against a real Postgres. tests/fixtures has fake statements, including PDFs
                     (made by tests/fixtures/make_pdf_fixtures.py, which needs reportlab and pypdf: development only)
```
