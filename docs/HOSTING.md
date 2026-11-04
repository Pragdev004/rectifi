# Put Rectifi online (and keep it private)

This guide moves Rectifi from your laptop to a small free server that is always on, so you can open it from your phone any time. It is written for someone who has never run a server. Go in order. Each step says what you should see.

**Be honest about what this is.** It is a hobby setup on a free cloud account. It is good enough for one person's expenses. It is not a bank. Read "Honest limits" at the end before you put real statements on it.

## What you are building

```
your phone / laptop  --(Tailscale, encrypted)-->  the server (Oracle VM)
                                                   |- app      (Rectifi web app, port 8501, this machine only)
                                                   |- gateway  (phone SMS gateway, port 8765, this machine only; optional)
                                                   |- db       (Postgres, no port open)
                                                   |- ollama   (the local model, no port open)
                                                   `- backup   (a database copy every day)
```

- The only way in is **Tailscale**, which is installed on the server. No port is open to the internet.
- **Tailscale Serve** (recommended): only devices signed in to *your* Tailscale account can reach Rectifi.
- **Tailscale Funnel**: anyone on the internet can reach the sign-in page. You need it if friends use Rectifi (they cannot join your tailnet), and for their phones to reach the SMS gateway. Without friends, skip it.
- Either way, Rectifi has its own **login**. The app refuses to start without one when it runs on the server: one password (one person), or invite codes (`RECTIFI_MULTIUSER=1`, several people).
- Two database logins: `postgres` (the superuser: set-up and backups only) and `rectifi` (the app). The app's login is a plain role on purpose: Postgres skips the privacy check between people for a superuser, so the app must never be one.

## Before you start

You need:

- an email address and a phone number for sign-ups;
- a debit or credit card for Oracle's identity check. Oracle says it does not charge the card for Always Free. **I could not confirm whether Indian debit or RuPay cards are accepted.** If yours is refused, try another card, or use the laptop-only setup (see "If you can't get Oracle");
- your Rectifi code in a Git repository you can clone from the server, or a way to copy the folder (`scp`, below);
- about 90 minutes the first time. Most of it is waiting for downloads.

Money: everything here is meant to cost 0 rupees. Oracle keeps pushing "Upgrade to a paid account". Do not. Stay on Always Free resources only.

## Step 1. Tailscale account

1. Make a free account at tailscale.com (the Personal plan).
2. Install Tailscale on your phone and laptop and sign in with the same account.
3. In the Tailscale admin page, open **DNS**. Make sure **MagicDNS** is on, and click **Enable HTTPS** (Serve and Funnel need it). You will see your tailnet name, like `tail1a2b3c.ts.net`.

## Step 2. Oracle account and the server

1. Sign up at oracle.com/cloud/free. Choose your **home region** carefully: you cannot change it later, and Always Free servers must live there. Pick the one closest to you that you can select (for example Mumbai or Hyderabad).
2. In the console: **Compute > Instances > Create instance**.
   - **Image:** Canonical Ubuntu 24.04 (the `aarch64` build).
   - **Shape:** change shape > **Ampere** > `VM.Standard.A1.Flex`, **2 OCPU, 12 GB memory**. As of the plan date (June 2026) that is the Always Free limit. Look for the "Always Free-eligible" label. If the console shows a price, you picked the wrong thing.
   - **Networking:** the default network is fine. Keep "assign a public IPv4 address" on (the server needs it to reach the internet, and so you can log in the first time).
   - **SSH keys:** choose "Generate a key pair" and download both files, or paste your own public key. **Keep the private key file safe.** Without it you cannot log in.
   - **Boot volume:** you can raise it to 100 GB or more (Always Free gives 200 GB in total).
   - **Advanced options > Management > Cloud-init script:** paste the whole of `deploy/cloud-init.yaml` from this repository. This installs Docker and Tailscale, adds swap and turns on automatic security updates. It opens no ports.
3. Click **Create**.

**If you see "Out of host capacity":** Oracle has no free Arm servers in your region right now. This is common. Try again later (morning in India time often works), or choose another availability domain in the same region. There is no trick that makes it work.

## Step 3. Log in and wait for setup

On your laptop, in a terminal (the key file name will differ):

```
chmod 600 ~/Downloads/ssh-key.key
ssh -i ~/Downloads/ssh-key.key ubuntu@<the public IP from the console>
```

Then on the server:

```
cloud-init status --wait
```

You should see `status: done`. This takes 3 to 8 minutes. If it says `error`, run `sudo less /var/log/cloud-init-output.log` and read the last lines.

Check the tools are there:

```
docker --version && docker compose version && tailscale version
```

If `docker` says "permission denied", log out and back in once (the setup added you to the `docker` group).

## Step 4. Sign in to Tailscale

```
sudo tailscale up --hostname=rectifi
```

It prints a link. Open it on your laptop and approve the new machine. Then get the machine's full name:

```
tailscale status --json | python3 -c "import sys,json; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))"
```

You will get something like `rectifi.tail1a2b3c.ts.net`. Write it down. That is `RECTIFI_HOSTNAME`.

Optional but good: `sudo tailscale set --ssh` lets you log in over Tailscale. Once that works you can delete the SSH rule in the Oracle network settings, so the server has no open ports at all.

## Step 5. Get the code onto the server

If the repository is on GitHub: `git clone <your repository URL> rectifi && cd rectifi`. A private repository needs a read-only access token as the password.

If you would rather copy it from your laptop, run this on the laptop (not the server):

```
rsync -av --exclude node_modules --exclude .venv --exclude .git --exclude .env ./ ubuntu@<ip>:rectifi/
```

The folder `web/dist` must be in it. The server has no Node, so it uses the build that is committed.

## Step 6. Fill in your settings

On the server, inside the `rectifi` folder:

```
cp .env.deploy.example .env.deploy
chmod 600 .env.deploy
python3 scripts/dev.py set-password --print
nano .env.deploy
```

- (If `dev.py` says "unrecognized arguments" for `--print`, run `python3 scripts/devtools/deploy.py set-password --print` instead.)
- `set-password --print` asks for your password twice, then prints two lines. The password must be at least 12 characters. Three or four random words works. It never shows or saves the password itself, only a scrambled copy.
- In `nano`, set `RECTIFI_HOSTNAME`. Set `POSTGRES_PASSWORD` to the output of `openssl rand -hex 24`, and `RECTIFI_DB_PASSWORD` to the output of a second `openssl rand -hex 24`. They must be different. Save with Ctrl+O, Enter, Ctrl+X.
- **One person:** paste the two lines from `set-password --print` over `RECTIFI_PASSWORD_HASH` and `RECTIFI_SESSION_SECRET`. Keep the single quotes around the hash.
- **Several people:** set `RECTIFI_MULTIUSER=1`, `RECTIFI_SESSION_SECRET` to the output of `openssl rand -hex 32` (32 characters or more), and `RECTIFI_BLIND_KEY` to the output of a second `openssl rand -hex 32` (or keep the one `set-password --print` printed). The blind key is required: the app will not start without it, and `deploy-check` and `doctor` fail. See "Back up the blind key" below. Leave `RECTIFI_PASSWORD_HASH` empty: it is ignored. There is no shared password. People sign in with their own name and password after you invite them (see "Invite people").

Now check everything:

```
python3 scripts/dev.py deploy-check
```

It checks the passwords (it never prints them), that `RECTIFI_DB_PASSWORD` differs from `POSTGRES_PASSWORD`, that the session secret and the blind key are 32 characters or more in multi-user mode, and that no port is published on every interface. Fix every line that says `FAIL`. Do not go on until it says `Ready to deploy.`

**Already ran an older version of this guide?** Your Docker volume was made when the database login `rectifi` was the superuser. That login skips the privacy check, so the app **refuses to start** in multi-user or public mode until you rebuild it. The init script runs only on a new volume. Do this once:

```
rc exec -T db pg_dump -U rectifi -Fc rectifi > old-volume.dump     # export first; keep the file safe
rc down -v                                                         # DELETES the database volume (and the model volume)
rc up -d --build                                                   # new volume: postgres + a plain rectifi role
```

Then restore `old-volume.dump` as in "Restore" below (with `-U postgres`). `down -v` also deletes the downloaded models; `ollama-pull` fetches them again.

## Step 7. Start Rectifi

To avoid typing a long command every time (`--profile gateway` also starts the phone SMS gateway; take it out of the alias if you do not want the gateway):

```
echo "alias rc='docker compose --env-file ~/rectifi/.env.deploy -f ~/rectifi/deploy/compose.oracle.yml --profile gateway'" >> ~/.bashrc && source ~/.bashrc
rc up -d --build
```

The first run builds the app image and downloads the models (about 4 GB). Expect 10 to 25 minutes on 2 slow cores. Watch it:

```
rc ps
rc logs -f ollama-pull
```

`ollama-pull` ends by itself with `Exited (0)`. That is correct. `app`, `db`, `ollama`, `backup` and (with the profile) `gateway` should say `Up` (and `healthy` for the first three and the gateway). Test from the server:

```
curl -s http://127.0.0.1:8501/api/health
curl -s http://127.0.0.1:8765/health
```

You should see `{"ok":true}` for each. Both ports are published on `127.0.0.1` only, never on the internet. Run **one** app worker: the compose file does, and sign-in limits, alerts and uploads live in that process's memory.

If `app` keeps restarting, run `rc logs app`. The usual cause is a missing setting: the message says which one and how to fix it.

## Step 8. Open it from your devices

**Private (recommended):**

```
sudo tailscale serve --bg 8501
tailscale serve status
```

Open `https://rectifi.tail1a2b3c.ts.net` (your name) on your phone or laptop with Tailscale switched on. You see the sign-in page. Type your password.

On your phone: in Chrome, menu, **Add to Home screen**.

**Public (needed for friends, and for phones that forward SMS):**

```
sudo tailscale funnel --bg 8501                  # the app, https://<name>.ts.net (port 443)
sudo tailscale funnel --bg --https=8443 8765     # the SMS gateway, https://<name>.ts.net:8443
tailscale funnel status
```

Tailscale may print a link to allow Funnel for your account; open it and approve. The same address now works from any device, with no Tailscale. In the SMS forwarder app on a phone, the URL is `https://<name>.ts.net:8443/sms`.

**Not verified:** whether Funnel is free on the Personal plan; that it accepts port 8443 for a second service next to 443; and what Host header the gateway sees through it (the gateway does not check Host, the app does and lists your name). None of this has run on a real account. If Funnel does not work for you, the fallback is a free tunnel, such as Cloudflare Tunnel, pointed at `127.0.0.1:8501` and `127.0.0.1:8765`. Add its public name to `RECTIFI_HOSTNAME`. Never publish those ports on `0.0.0.0` yourself.

What changes with Funnel:

- Anyone on the internet can see the sign-in page and try passwords. Use a long password. In one-person mode Rectifi allows 5 wrong tries a minute, then slows down, then **locks sign-in for 15 minutes after 10 wrong tries**. That lock is global, so a stranger can also lock *you* out. In multi-user mode nothing locks an account. Wrong tries are counted per account and per browser: a browser that has signed in before carries a signed cookie, so a stranger who guesses your username cannot make *that* browser wait. A brand-new browser behind the same address as the stranger can be held out for 15 minutes; your recovery code works at once from it and marks it as known.
- **Real client addresses.** Funnel, Docker and Serve all reach the app from one address (127.0.0.1 or the Docker bridge), so by default every stranger looks like one client. If your proxy sends the real address in `X-Forwarded-For`, list the proxy's own address in `RECTIFI_TRUSTED_PROXIES` (for example `127.0.0.1`, or `172.16.0.0/12` for the Docker bridge). The header is believed only from those peers; leave it empty if you are not sure. I could not confirm what Funnel sends, so the default is to ignore it. uvicorn's own proxy-header handling is off on every launch path (`--no-proxy-headers`), so this setting is the only thing that can change the address.
- The gateway answers only `/health` and `POST /sms` with a device token. It returns no data. Tokens are stored as hashes. Make a token for each phone on the Phone page of the app.
- I could not confirm from Tailscale's plan pages that Funnel is free on the Personal plan. The command tells you if it is not.

To go back to private: `sudo tailscale funnel reset`, then `sudo tailscale serve reset`, then run the `serve` command again.

**Funnel from your laptop (not the server).** On the server the password is always on. On your laptop it is not: the laptop app has no login unless you set `RECTIFI_PUBLIC=1`. So `tailscale funnel 8501` on the laptop would put your bank data on the internet for anyone with the link. Rectifi checks for this. If Funnel is on for the app and there is no login, `python scripts/dev.py up` refuses to start and `python scripts/dev.py doctor` fails with "Funnel is on: set RECTIFI_PUBLIC=1 and run `python scripts/dev.py set-password` first". Do that (put the two lines it prints, plus `RECTIFI_PUBLIC=1`, in `.env`), or switch Funnel off with `tailscale funnel reset`. The check reads `tailscale serve status --json`; if Tailscale is not installed or its answer looks different from what Rectifi expects, it stays quiet and does nothing. It has not been run against a real Tailscale install.

## Using it

- The first time, the database is empty. Add accounts, then import a statement or add entries as on your laptop.
- To move your laptop data to the server, see "Move your laptop data".
- The **phone SMS gateway runs on the server only when you start it** (`--profile gateway`). It is for multi-user mode, so friends' phones can post bank SMS while your laptop is off. No model ever sees an SMS.
- The old **Streamlit app and the MCP server refuse to start** in multi-user mode. They have no sign-in. Use the web app.
- Model replies are slow on this server (see "Honest limits"). Everything that does not use the model is fast.

### Move your laptop data

On the laptop (Postgres in Docker or native). First make sure the laptop database is the new kind: it must have been through `python scripts/dev.py migrate-tenancy` (see "Invite people"). An old-style dump would make the server refuse to start.

```
pg_dump -Fc "postgresql://rectifi:rectifi_dev@localhost:5432/rectifi" > rectifi-laptop.dump
scp rectifi-laptop.dump ubuntu@<ip>:
```

On the server, follow "Restore" below using that file. Do this once, early.

## Backups

The `backup` container writes `/var/backups/rectifi/rectifi-YYYY-MM-DD.dump` every 24 hours (first one right after start) and keeps the newest 7.

Check they exist:

```
ls -lh /var/backups/rectifi
```

**Back up the blind key (`RECTIFI_BLIND_KEY`) apart from these backups.** Several-people mode makes keyed hashes of UPI references and SMS (they find duplicates without storing the text). The hashes use the people's salts, which are in the database, and this key, which is not. That is the point: a stolen dump or backup alone cannot be used to guess them. So never put the key in the same place as the dumps. Keep a copy in your password manager (or somewhere else that is not the backup folder). If you lose it, nothing is lost: set a new one, restart, and each person's hashes are made again from their own data the next time they sign in (until then a re-sent SMS can look new once, and is merged afterwards). Note that whoever holds both the key and a dump can test guesses against those hashes, so protect the key like a password.

**The backups run as `postgres`.** The app's own login is a plain role, and a plain role cannot dump tables that row level security forces. The backup container uses `POSTGRES_PASSWORD`; nothing else needs it.

**What a backup holds (the privacy side).** A dump is a full copy of the database. In multi-user mode people's text (payees, notes, narrations) is stored encrypted, but amounts and dates are plain, so treat a dump as private. Rectifi keeps **7 days** of dumps. When someone deletes their account, their rows are gone from the live database at once, but they stay in the backups for up to 7 days. The privacy page tells people this. Do not copy dumps anywhere you would not keep the database itself.

**A copy on the same server is not a real backup.** If the server is taken back by Oracle you lose both. Once a week, copy the newest file to your laptop (run on the laptop):

```
scp ubuntu@rectifi:/var/backups/rectifi/rectifi-2026-10-25.dump .
```

(`ubuntu@rectifi` works when SSH over Tailscale is on; otherwise use the IP.) Oracle's block-volume backups (5 free) are a second safety net: in the console, open the boot volume and create a backup before big changes.

## Restore

Replace the file name with yours. This **replaces everything in the database** with the backup. It runs as `postgres` and hands every table to the `rectifi` role (`--role=rectifi`), so the app can use them.

```
cd ~/rectifi
rc stop app gateway
rc exec -T db pg_restore -U postgres -d rectifi --clean --if-exists --no-owner --role=rectifi < /var/backups/rectifi/rectifi-2026-10-25.dump
rc start app gateway
```

(`--role=rectifi` is my reading of what keeps the tables owned by the app role. It has not been run against a real server. After a restore, check that you can sign in and see your data. If the app says it cannot read a table, run `rc exec db psql -U postgres -d rectifi -c "reassign owned by postgres to rectifi"` and restart.)

You may see a few "does not exist, skipping" notes on a fresh database. Those are fine. Open Rectifi and check your latest entries.

**Test a restore once, now, while nothing depends on it.** An untested backup is a hope, not a backup. Easiest test: restore into a scratch database instead of the real one:

```
rc exec db createdb -U postgres scratch
rc exec -T db pg_restore -U postgres -d scratch --no-owner < /var/backups/rectifi/<a-file>.dump
rc exec db psql -U postgres -d scratch -c "select count(*) from transactions"
rc exec db dropdb -U postgres scratch
```

## Invite people (multi-user mode)

Do this once, in this order. Run every command inside the app container: `rc exec app python scripts/dev.py <command>` (not verified on a real server: if `dev.py` cannot find its Python there, run the same command on a machine that has the code and a `DATABASE_URL` for this database).

1. **Back up first.** Take a dump (see "Backups") and copy it off the machine. The migration changes every table.
2. **Migrate the database (you run this yourself).** `python scripts/dev.py migrate-tenancy` gives every existing row to the owner (user 1). It shows what it will change and waits for you to type `yes`. It needs a terminal. Nothing runs it for you, and Rectifi will not start on an old database until you do. On a brand new server database there is nothing to migrate.
3. **Make the owner invite.** `python scripts/dev.py invite --owner` prints a one-time code. It is shown **once** (only a hash is stored). Copy it now.
4. **Sign up as the owner.** Open the app, choose sign up, paste the code, pick a name and a password. Save the recovery code the page shows. Finish sign-up.
5. **The owner sweep runs.** After you finish, Rectifi encrypts your old text in the background. It can take a while on a big ledger and picks up where it stopped if the app restarts. To see whether it finished, look in the app logs (`rc logs app`) or check the `users` table: `sweep_state` is `done` for the owner when it has finished (`rc exec db psql -U postgres -d rectifi -c "select id, sweep_state from users"`).
6. **Clean up old copies.** Before the sweep, your old text was plain, and it survives in dead rows and in the old backups. After the sweep says done: run `rc exec db psql -U postgres -d rectifi -c "VACUUM FULL"`, then **delete every backup older than the day the sweep finished** (`ls -l /var/backups/rectifi`, then `rm` the old ones, including the one from step 1 once you trust the new state). Keep only dumps made after the sweep. Friends never have this gap: their text is encrypted from the first row.
7. **Invite a friend.** `python scripts/dev.py invite` prints a one-time code (default valid 7 days; `--days 3` changes it). Send it to them in a private message. They open your address, choose sign up, and finish. A code works once.
8. **See and cancel.** `python scripts/dev.py invites` lists invites and their state (never a code). `python scripts/dev.py revoke-invite <id>` cancels one that is unused.

**There is no password reset.** If a friend forgets their password and loses their recovery code, their text cannot be read again, by them or by you. That is on purpose: it is what keeps you out of their data. Nothing in the repository resets a password for them. They can sign up again with a new invite.

**Deleting the owner account** is done on the server only: stop Rectifi, then run `python scripts/dev.py reset-owner` in a terminal. It refuses while the app, the gateway or any other database connection is open, asks for the owner's username and the word `delete`, and removes every owner row (including the chat memory and all chat threads) in one transaction. Old backups keep the owner's chat and charts readable until they are deleted (up to 7 days).

Friends see only their own pages (no Ask, Charts, Library, Memory or Traces). The owner keeps those. A friend can export or delete their account from Settings.

## Updates

Your app:

```
cd ~/rectifi
git pull
python3 scripts/dev.py deploy-check
rc up -d --build
```

If the new version misbehaves, go back: `git log --oneline`, then `git checkout <the older commit>` and `rc up -d --build`. The database is not changed back, so take a backup first before big updates (`rc exec -T db pg_dump -U postgres -Fc rectifi > before-update.dump`).

The operating system installs security updates by itself. Some need a restart now and then: `sudo reboot`. Everything comes back on its own (the containers restart, and Tailscale remembers `serve`).

Docker images: Postgres, Ollama and Python are pinned to versions. To move to a newer Ollama, change `OLLAMA_VERSION` in `.env.deploy` (look at hub.docker.com/r/ollama/ollama/tags first) and run `rc up -d`.

## Change the password, sign everyone out, or unlock

- **Change the password:** `python3 scripts/dev.py set-password --print`, paste the two new lines into `.env.deploy`, then `rc up -d app`. The new session secret signs every device out.
- **Locked out for 15 minutes:** wait. To clear it now: `rc exec db psql -U postgres -c "delete from app_settings where key='login_lock'"` and `rc restart app`.
- **A phone was lost:** change the password as above.

Note: `rc restart app` does not re-read `.env.deploy`. Use `rc up -d app` after editing it.

## If you can't get Oracle

- **Your laptop, same files:** you can run the same Docker setup on your own computer, or just `python scripts/dev.py up` and `python scripts/dev.py tailscale`. It is free and the data never leaves your machine. It only works while the laptop is on.
- **Always-on without a model:** a free web host plus a free hosted Postgres can run the app, but both sleep after a few minutes, are tiny, and cannot run Ollama. I do not recommend it for bank data.
- Hugging Face Docker Spaces now need a paid plan, and the other free hosts I checked (Koyeb, Fly, Railway) are no longer free for new users.

## Honest limits

- **Oracle may take the server back.** Free servers that look idle for 7 days (low CPU, network and memory) can be reclaimed. The model stays loaded (`OLLAMA_KEEP_ALIVE=-1`), which should keep memory use up. That is my reading of Oracle's rule, and I have not tested it. Keep your off-server backups.
- **Capacity.** "Out of host capacity" can stop you creating the server at all.
- **One server is one point of failure.** If it dies, you restore from a backup.
- **It is slow.** My estimates, not measurements: a 3B model on 2 Arm cores writes about 5 to 8 words a second. A chart attempt can take a minute or more. A full "look it up" answer can take 1 to 3 minutes. Bigger models (7B) fit in memory but are 2 to 3 times slower. Classification by rules and past choices is instant.
- **Small models are less accurate.** The checks that protect your numbers (the amount guard, reconciliation) do not depend on the model. Wrong categories are fixable, and Rectifi learns from your fixes.
- **Not tested here.** The files in `deploy/` were checked with `docker compose config`, a Dockerfile linter and Python tests. They were **not** built or run on a real Docker server, an Oracle VM or Funnel. Expect to fix small things on the first run, such as a package that has no Arm build. If you do, tell me what the error said.
- **Your data is on a machine you don't control.** The disk is not encrypted by you, and Oracle staff could in principle read it. If that bothers you, do not use a hosted setup; use your laptop.
- **The password is the only lock behind Funnel.** There is no two-step sign-in. Use Serve if you can.
- **No password reset email.** In one-person mode, if you forget the password, run `set-password` on the server as above. In multi-user mode there is no reset at all (see "Invite people").
- **Funnel, the public gateway and real phones are untested.** No phone has posted to the gateway through Funnel. Banks word SMS differently, and phone battery savers may stop the forwarder app. Treat auto-capture on a friend's phone as "try it and see".
- **Old plain text can linger.** After the owner sweep, run the clean-up in "Invite people". Until then, plain text of your old data can sit in old backups, dead rows and the write-ahead log.
- **Uploads are limited to 10 MB**, as on your laptop. Run only one app worker (the compose file does).
