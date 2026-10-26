# n8n automation: bookings and handoffs

When the assistant books an appointment or a patient asks for a person, the app sends an event to n8n. n8n then:

- **Booking:** adds a row to a Google Sheet and emails the front desk.
- **Handoff:** emails the front desk the patient's details and a summary of the conversation, so someone can call back.

```
app (booking.notify)  --POST + X-Webhook-Secret-->  n8n webhook  -->  Which event?
                                                                      ├─ Booked  -> Booking row -> Google Sheet
                                                                      │           -> Booking email -> Gmail
                                                                      └─ Handoff -> Handoff email -> Gmail
```

Files in this folder:

| File | What it is |
| --- | --- |
| `n8n_clinic_events.json` | The workflow. Import it into n8n. |
| `bookings_sheet_headers.csv` | The header row for the Google Sheet. |
| `build_workflow.py` | Generates the JSON (edit this, then run `python automation/build_workflow.py`). |

The setup takes about 30 minutes the first time. Most of that is Part 4, the Google sign-in setup, which you only do once.

---

## Part 1: Install Node.js (n8n runs on it)

1. Go to **https://nodejs.org** and download the **LTS** installer for Windows. n8n needs Node.js 20.19 to 24.
2. Run it with the default options.
3. Open a **new** PowerShell window and check:
   ```powershell
   node --version
   ```
   You should see `v22...` or `v24...`.

## Part 2: Start n8n

In PowerShell (any folder):

```powershell
npx n8n
```

The first run downloads n8n (a few minutes). When it prints `Editor is now accessible via: http://localhost:5678`, open that address in your browser and create the owner account. The account is local to your PC, so any email and password work.

**Keep this PowerShell window open.** Closing it stops n8n. Next time, run `npx n8n` again and your workflows will still be there.

## Part 3: Make the Google Sheet

1. Go to **https://sheets.new** to create a sheet. Name it something like `Quillmere bookings`.
2. Rename the tab at the bottom from `Sheet1` to **`Bookings`** (right-click the tab, then Rename). The workflow looks for that exact name.
3. Paste this into cell **A1**. It should split into nine columns. If it doesn't, use Data → Split text to columns.
   ```
   Booked at,Appointment,Visit,Therapist,Patient,Phone,Email,Reason,Appointment ID
   ```
4. Copy the sheet's address from the browser bar. You'll paste it into n8n in Part 5.

## Part 4: Let n8n use your Google account (one time)

n8n on your PC needs its own "app" registered with Google before it can send Gmail and write to Sheets.

1. Go to **https://console.cloud.google.com** and sign in with the Google account that owns the sheet.
2. At the top, click the project picker, then **New project**. Name it `clinic-concierge-n8n` and click **Create**. Make sure it's selected afterwards.
3. Go to **APIs & Services → Library**. Search for and **Enable** each of these:
   - **Gmail API**
   - **Google Sheets API**
   - **Google Drive API** (Sheets needs it too)
4. Go to **APIs & Services → OAuth consent screen** and click **Get started**.
   - App name: `Clinic Concierge n8n`. Support email: yours. Click **Next**.
   - Audience: **External**. Click **Next**.
   - Contact email: yours. Accept the policy, then click **Create**.
5. Still on that screen, open **Audience → Test users → Add users**, add **your own Gmail address**, and save. While the app is in "Testing", only these users can sign in.
6. Go to **APIs & Services → Credentials → + Create credentials → OAuth client ID**.
   - Application type: **Web application**. Name: `n8n local`.
   - **Authorized redirect URIs → Add URI:**
     ```
     http://localhost:5678/rest/oauth2-credential/callback
     ```
   - Click **Create**. A box shows a **Client ID** and **Client Secret**. Leave it open, or download the JSON, because you need both in Part 5.

## Part 5: Import and connect the workflow

1. In n8n, open **Overview**, then use the **⋯** menu or **Create** to **Import from file**, and choose `automation/n8n_clinic_events.json`.
2. **Settings** step: double-click it and change the two values:
   - `staff_email`: the address that should get the emails (yours, for the demo).
   - `sheet_url`: the sheet address from Part 3.
3. **Add to Bookings sheet** step: double-click it, then under **Credential** choose **Create new credential** (Google Sheets OAuth2 API).
   - Paste the **Client ID** and **Client Secret** from Part 4. The OAuth Redirect URL shown there should match the one you entered in Google.
   - Click **Sign in with Google** and choose your account.
   - Google warns **"Google hasn't verified this app"**. That's expected for your own test app: click **Continue** and allow access.
   - Click **Save**.
4. **Email the front desk** step: do the same, creating a **Gmail OAuth2** credential with the same Client ID and Secret.
5. **Email the front desk (handoff)** step: choose the Gmail credential you just made. There's no need to create it again.
6. **Clinic events** step (the webhook): under **Credential for Header Auth**, choose **Create new credential**:
   - **Name:** `X-Webhook-Secret`
   - **Value:** a long random string. To make one, run this in PowerShell and copy the output:
     ```powershell
     [guid]::NewGuid().ToString() + [guid]::NewGuid().ToString()
     ```
   - Save it, and keep the value for Part 6.
   - While you're in this step, copy the **Production URL**. It looks like `http://localhost:5678/webhook/clinic-events`.
7. Click **Publish** (top right). The webhook only listens on its Production URL while the workflow is published.

## Part 6: Point the app at n8n

Add these lines to the project's `.env`. Use your own secret, never this example:

```
EVENT_WEBHOOK_URL=http://localhost:5678/webhook/clinic-events
EVENT_WEBHOOK_SECRET=paste-the-same-value-as-the-n8n-header-auth-credential
```

Then send test events, with no booking needed:

```powershell
python -m app.notify            # a sample booking: you should get an email and a new sheet row
python -m app.notify handoff    # a sample "patient asked for a person" email
```

Each prints `n8n answered HTTP 200 (OK)` when it works. In n8n, the **Executions** tab shows every run, step by step, including failures.

Finally, restart the app (`uvicorn app.main:app --reload`), book an appointment in the widget, and check your inbox and the sheet.

## Troubleshooting

| What you see | Why, and the fix |
| --- | --- |
| `HTTP 403` | The secret in `.env` doesn't match the n8n Header Auth credential, or the name isn't exactly `X-Webhook-Secret`. |
| `HTTP 404` | The workflow isn't published, or you used the **Test** URL (`/webhook-test/...`). The Test URL only works for one event after clicking **Execute workflow**. |
| `No answer` | n8n isn't running (its PowerShell window was closed). Run `npx n8n` again. |
| `redirect_uri_mismatch` (Google) | The redirect URI in Google Cloud doesn't exactly match the one n8n shows. Copy it again. |
| `access_denied` (Google) | Your Gmail address isn't in **Test users** (Part 4, step 5). |
| Worked last week, now a Google error | In Testing mode Google expires the sign-in after 7 days. Open the credential in n8n and click **Sign in with Google** again. |
| Sheet step fails with "Sheet not found" | The tab must be named exactly `Bookings`. |

## How it's protected

- **Shared secret:** the app sends `EVENT_WEBHOOK_SECRET` in an `X-Webhook-Secret` header, and n8n rejects any request without it (HTTP 403). Anyone who finds the webhook URL still can't post fake bookings.
- **Plain-text emails:** a patient's typed reason can't become clickable links or HTML in the staff inbox.
- **Email first, sheet second:** if the sheet step fails (renamed tab, expired Google sign-in), the run carries on and the staff email still goes out. The failure shows in n8n's Executions tab.
- **Never blocks the patient:** the app sends events in the background and retries 3 times if n8n is down. A notification problem never stops a booking, and the booking is in Postgres either way.
