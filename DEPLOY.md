# Deploying the live demo (Google Cloud Run + Groq + Supabase)

What runs where:

| Piece | Where | Cost |
| --- | --- | --- |
| API, chat widget and demo site | Google Cloud Run, `us-west1`, scales to zero | $0 within the monthly free tier |
| Answers and booking agent | Groq, `openai/gpt-oss-120b` (paid Developer plan) | about a tenth of a cent per message |
| Database (documents, calendar, bookings) | Supabase, the project you already have | free tier |
| Staff notifications (n8n) | a second Cloud Run service, with its data in Supabase (Part 8) | $0 within the free tier |

Pick the Cloud Run region closest to your Supabase region. Each message makes several database queries, so the distance adds up. This guide uses `us-west1` (Oregon) for a Supabase project in `aws-*-us-west-2` (also Oregon). For a Supabase project in `us-east-1`, use `us-east1` everywhere below.

Your PC keeps using Ollama. Only the live demo uses Groq, and you choose which with `LLM_PROVIDER`.

**Why Groq on a paid plan?** We tried the free options first:
- Groq's free Llama models were removed in August 2026.
- Gemini's free tier turned out to allow only **20 requests a day per model** (5-10 chat messages), and it often stalled.

A public demo that says "busy" to a potential client costs more than the model does. On Groq's paid plan, gpt-oss-120b costs roughly $0.15 / $0.60 per million tokens, about a tenth of a cent per message, and it answers in a second or two. `LLM_PROVIDER=gemini` still works if you turn on Gemini billing instead.

The first time takes about 45 minutes. After that, redeploying is one command.

---

## Part 1: Groq API key and paid plan, tested on your PC first

1. In **https://console.groq.com**, create a key under **API Keys** (`gsk_...`), or reuse the one you have.
2. Go to **Settings → Billing** and upgrade to the **Developer** plan with a card. The free tier allows only about 8,000 tokens a minute, which is about 2 messages.
3. In your `.env`:
   ```
   LLM_PROVIDER=groq
   GROQ_API_KEY=gsk_...
   ```
   The model defaults to `openai/gpt-oss-120b`, with `openai/gpt-oss-20b` as the fallback.
4. Check it: `python -m app.llmcheck` should pass all 5 steps. Then use `python -m app --debug`: ask "do you take Aetna?" and make a booking.
   - If a model name is ever retired, the error lists the ones your key can use. Set `GROQ_MODEL=...` in `.env` and in `deploy.env.yaml`.
5. Switch back to `LLM_PROVIDER=ollama` when you want to work offline for free.

Never paste the key into a chat or commit it to git. It belongs only in `.env` and `deploy.env.yaml`, and both are ignored by git.

## Part 2: Check your database address works from the cloud

Cloud Run can only reach Supabase through its **pooler** address (Supabase's direct address is IPv6-only).

In Supabase: **Connect** (top of your project) → **Session pooler**. The address looks like:
```
postgresql://postgres.abcdefghij:[YOUR-PASSWORD]@aws-0-us-east-1.pooler.supabase.com:5432/postgres
```
If the `DATABASE_URL` in your `.env` already contains `pooler.supabase.com`, it's the right one. If it contains `db.xxxx.supabase.co`, copy the Session pooler one instead.

## Part 3: Google Cloud project and billing

1. In **https://console.cloud.google.com**, use the project picker to create a **New project** called `clinic-concierge`, then select it. You can reuse the n8n one, but a separate project keeps things tidy.
2. **Billing → Link a billing account → Create billing account**. You need a card, but the demo stays inside the free tier (see "Costs" below). If Google offers the free trial credit, accept it.
3. **Set a budget alert**, so you'd hear about any charge right away: **Billing → Budgets & alerts → Create budget**.
   - Amount: **$1**
   - Alerts at **50% and 100%**, emailed to you

## Part 4: Install the gcloud command-line tool (Windows)

1. Download and run the installer: **https://cloud.google.com/sdk/docs/install** (Windows → "Google Cloud CLI installer"). Keep the default options.
2. At the end it opens a terminal and runs `gcloud init`:
   - Log in with your Google account (a browser window opens).
   - Pick the `clinic-concierge` project.
   - If it asks for a default region, choose `us-west1`.
3. Open a **new** PowerShell window and check:
   ```powershell
   gcloud --version
   gcloud config get-value project      # should print your project id
   ```
4. Turn on the three services the deploy uses. This takes about a minute:
   ```powershell
   gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
   ```

## Part 5: The settings file

In the project folder:
```powershell
Copy-Item deploy.env.example.yaml deploy.env.yaml
notepad deploy.env.yaml
```
Fill in `DATABASE_URL` (the pooler address from Part 2) and `GROQ_API_KEY`, keeping the quotes. Use the format `NAME: "value"`, with a colon and a space, not `=`. Save.

`deploy.env.yaml` is listed in `.gitignore`, `.gcloudignore` and `.dockerignore`, so it never gets committed, uploaded with the code, or built into the image. The deploy command sends its values to Cloud Run as settings.

## Part 6: Deploy

From the project folder (venv not needed):
```powershell
gcloud run deploy clinic-concierge `
  --source . `
  --region us-west1 `
  --allow-unauthenticated `
  --memory 2Gi --cpu 1 `
  --min-instances 0 --max-instances 1 `
  --concurrency 10 --timeout 120 `
  --cpu-boost `
  --env-vars-file deploy.env.yaml
```
The backticks at the ends of lines are PowerShell's line continuation, so paste the whole block at once.

- If it asks to create an Artifact Registry repository, answer **Y**.
- Google builds the Docker image in the cloud (about 5–10 minutes the first time; it downloads the AI models into the image), then starts it.
- At the end it prints **Service URL: https://clinic-concierge-xxxxx-ue.a.run.app**. That's your live demo.

What the options mean:

| Option | Why |
| --- | --- |
| `--allow-unauthenticated` | Anyone with the link can open it. It's a public demo. |
| `--memory 2Gi` | The search models need about 600 MB; 2 GB leaves room. |
| `--min-instances 0` | Shuts down when nobody's using it, so it costs nothing while idle. The first visit after a pause takes about 20–40 s to wake up. |
| `--max-instances 1` | Never more than one server: caps cost, and keeps the message limits accurate (they're counted in memory). |
| `--cpu-boost` | Extra CPU while starting up, so it wakes faster. |

## Part 7: Check it

1. Open `https://YOUR-URL/health`. It should show `{"status":"ok","model":"openai/gpt-oss-120b"}` (or the model you set).
2. Open `https://YOUR-URL/`. You should see the Quillmere site. Ask a question, then make a booking.
3. Something wrong? Read the server's log:
   ```powershell
   gcloud run services logs read clinic-concierge --region us-west1 --limit 50
   ```
   Or in the console: **Cloud Run → clinic-concierge → Logs**.

## Part 8: n8n in the cloud (staff emails and the Google Sheet)

On your PC, n8n sends the staff emails and adds the sheet rows, but the live site can't reach your PC. This part runs n8n as a second Cloud Run service. Its data goes in your Supabase database, so nothing is lost when it shuts down.

How it stays free: Cloud Run's default billing only charges, and only gives the server processing power, **while it's answering a request**. The workflow's webhook therefore replies only after the email and sheet row are done (`responseMode: lastNode`). The app sends the event in the background and waits up to 90 s, so a sleeping n8n has time to wake up without the patient waiting.

**1. Give n8n its own schema in Supabase.** In Supabase: **SQL Editor → New query**, run:
```sql
create schema if not exists n8n;
```

**2. The settings file.** In the project folder:
```powershell
Copy-Item n8n.env.example.yaml n8n.env.yaml
[guid]::NewGuid().ToString() + [guid]::NewGuid().ToString()     # copy this as the encryption key
gcloud projects describe clinic-concierge --format="value(projectNumber)"    # your project number
notepad n8n.env.yaml
```
Fill in:
- the Supabase user (`postgres.<project ref>`) and password, the same ones as in `DATABASE_URL`
- the encryption key
- the project number in the three addresses, e.g. `n8n-288399809809.us-west1.run.app`

Keep the file safe. If the encryption key changes, n8n can't read its saved Google sign-ins.

**3. Deploy n8n** (the same n8n version as on your PC):
```powershell
gcloud run deploy n8n `
  --image docker.io/n8nio/n8n:2.44.6 `
  --region us-west1 `
  --allow-unauthenticated `
  --port 5678 `
  --memory 2Gi --cpu 1 `
  --min-instances 0 --max-instances 1 `
  --timeout 300 `
  --no-cpu-throttling `
  --env-vars-file n8n.env.yaml
```
`--no-cpu-throttling` keeps the CPU on while n8n is awake. n8n finishes starting up in the background after it opens its port; with the CPU cut between requests it never finishes, and every page says `Cannot GET /`.
It prints `Service URL: https://n8n-…run.app`, which should match the address in your file. The first start takes a minute while n8n sets up its tables in Supabase.

**4. Claim it straight away.** Open the n8n address and create the owner account with a **strong, unique password**. Until an owner exists, anyone who finds the address could create one.

**5. Let Google accept the new address.** In Google Cloud (the project that has your OAuth client): **APIs & Services → Credentials → n8n local**:
- **Authorized JavaScript origins:** add `https://n8n-XXXX.us-west1.run.app`
- **Authorized redirect URIs:** add `https://n8n-XXXX.us-west1.run.app/rest/oauth2-credential/callback`

Keep the `localhost` entries too, so your PC's n8n keeps working. Save. Changes can take a few minutes to apply.

**6. Set up the workflow,** the same as on your PC (automation/README.md, Part 5):
- import `automation/n8n_clinic_events.json`
- fill in the **Settings** step
- create the Google Sheets and Gmail credentials, using **Sign in with Google**
- create the Header Auth credential (`X-Webhook-Secret`) with the **same secret** as in `.env`
- **Publish**

**7. Point the live site at it.** In `deploy.env.yaml` add:
```yaml
EVENT_WEBHOOK_URL: "https://n8n-XXXX.us-west1.run.app/webhook/clinic-events"
EVENT_WEBHOOK_SECRET: "the-same-secret"
```
Redeploy the app with the usual command (Part 6).

**8. Test.** From your PC, send a sample event straight to the cloud n8n (setting the variable in the window overrides `.env`):
```powershell
$env:EVENT_WEBHOOK_URL="https://n8n-XXXX.us-west1.run.app/webhook/clinic-events"; python -m app.notify
Remove-Item Env:EVENT_WEBHOOK_URL
```
Then book through the live site: the email and the sheet row should arrive within about a minute (longer if n8n was asleep).

**Failure alerts.** The same workflow emails `alert_email` (Settings step) when a visitor gets an error instead of an answer: what broke, the likely fix, and the log command. Test it with `python -m app.notify error`. `ALERT_EVERY_MINUTES` (default 30) limits it to one email per kind of problem in that window.

**Notes**
- **Cost:** with the CPU always on, n8n is billed for as long as it's awake. It wakes for a booking or for the editor and goes back to sleep after about 15 minutes of no requests, so each wake-up costs roughly 2 cents. That billing mode's free tier (240,000 vCPU-seconds and 450,000 GiB-seconds a month) covers about 250 wake-ups. **Close the n8n tab when you're done:** an open editor keeps it awake.
- **Google's 7-day sign-out:** the OAuth app is in "Testing", so Google signs you out of the cloud n8n after 7 days, as it does locally. Open the credential and click **Sign in with Google** again. Publishing the OAuth app removes the limit, but Google may ask you to verify it.
- **Two copies of the workflow:** your PC and the cloud each have one. Changes made in one aren't copied to the other; re-import the JSON to bring them in line.

## Updating, pausing, removing

- **Changed code?** Run the same deploy command again. Settings are kept.
- **Change one setting** (for example the daily limit):
  `gcloud run services update clinic-concierge --region us-west1 --update-env-vars MESSAGES_PER_DAY=500`
- **Take it offline:** `gcloud run services delete clinic-concierge --region us-west1`

## Costs

- **Cloud Run:** you pay only while a request is being handled (plus start-up). The monthly free tier is 180,000 vCPU-seconds, 360,000 GiB-seconds and 2 million requests. A demo that handles a few hundred chats a month uses well under 1% of it. The budget alert from Part 3 tells you if anything is ever charged.
- **Artifact Registry:** each deploy stores an image of about 1 GB. Above 0.5 GB the storage costs roughly $0.10 per GB per month, so delete old images now and then: **Artifact Registry → cloud-run-source-deploy → clinic-concierge** → select the old ones → Delete.
- **Groq:** about a tenth of a cent per message for gpt-oss-120b. `MESSAGES_PER_DAY` (300) caps what strangers can spend at roughly 30-40 cents a day. The Groq console shows usage. If a limit is ever reached, the chat says "The assistant is busy right now" instead of failing.
- **Supabase:** free projects **pause after a week with no activity**. If the demo stops answering with a database error, open the Supabase dashboard and click **Restore project**.

## Troubleshooting

| What you see | Why, and the fix |
| --- | --- |
| Build fails at `python -m app.warm` | The model download from Hugging Face failed. Run the deploy again; it's usually temporary. |
| `/health` never loads; log says `Memory limit exceeded` | Raise memory: `gcloud run services update clinic-concierge --region us-west1 --memory 4Gi`. |
| Log says `password authentication failed` or `could not translate host name` | `DATABASE_URL` is wrong, or it's the direct address. Use the Session pooler one (Part 2), then redeploy. |
| Chat says "The assistant isn't available right now" | The log shows the real reason, usually a wrong `GROQ_API_KEY` or a retired model name. The log lists the valid names. Run `python -m app.llmcheck` locally to test the key and model. |
| Chat says "busy right now" | A provider limit was reached. On Groq's free tier this happens after about 2 messages a minute, so check the account is on the paid Developer plan. On Gemini's free tier the limit is 20 requests a day. |
| Some replies take 30+ s, or the log shows `took too long to answer` | The provider stalled on a request. The app gives up (30 s on Groq, 20 s on Gemini), retries once, then the fallback model answers (`GROQ_FALLBACK_MODEL` / `GEMINI_FALLBACK_MODEL`). If it happens a lot, compare models with `python -m app.llmcheck`. |
| First message takes 30 s | The demo was asleep and is waking up (`--min-instances 0`). Later messages are fast. |
