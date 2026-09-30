# Talk with Reachy: study setup

This repository is a fork of Pollen Robotics' [Reachy Mini conversation app](https://github.com/pollen-robotics/reachy_mini_conversation_app). The conversation behavior is unchanged. The fork adds two things:

1. **Transcript logging.** Every finished utterance, what people say to Reachy and what Reachy says back, is written to a file on the robot.
2. **OneDrive upload.** The robot copies those files to a UC OneDrive account every five minutes.

The Python package is renamed to `talk_with_reachy` because Reachy Mini installs all apps into one shared environment. With the upstream name, installing this app would replace the official conversation app on the same robot.

## What is recorded

One JSONL file per app run, stored at `/home/pollen/talk_with_reachy_data/transcripts/` on the robot and named `<robot>_<YYYYMMDD-HHMMSS>_<id>.jsonl`. Each line is one JSON record:

```json
{"session_id": "9f2c…", "type": "session_start", "time": "2026-10-01T09:00:12.345-04:00", "robot_id": "reachy-mini", "app_version": "1.0.1"}
{"session_id": "9f2c…", "type": "utterance", "seq": 1, "time": "2026-10-01T09:00:20.101-04:00", "speaker": "person", "text": "Hi Reachy, what are you doing?"}
{"session_id": "9f2c…", "type": "utterance", "seq": 2, "time": "2026-10-01T09:00:22.870-04:00", "speaker": "reachy", "text": "Just looking around! Want to see a dance?"}
{"session_id": "9f2c…", "type": "session_end", "time": "2026-10-01T11:45:03.002-04:00", "utterances": 2}
```

- `speaker` is `person` for any human and `reachy` for the robot. The app cannot tell people apart, so it records no identity.
- `time` is local time with its UTC offset.
- Only final transcripts are logged. Partial, in-progress text is not.
- **No audio and no video are stored.**

The transcripts come from the realtime speech backend. By default (`HF_REALTIME_CONNECTION_MODE=deployed`) that is a service Pollen Robotics hosts on Hugging Face, which means microphone audio leaves the robot to be transcribed. This happens in the upstream app too. The IRB application should say so. To keep audio on your own hardware, use `local` mode with your own [speech-to-speech](https://github.com/huggingface/speech-to-speech) server (see the upstream README).

## Setup, in order

### 1. Update the robot

This app needs `reachy-mini` 1.10.0rc5 or newer. Update the robot from Reachy Mini Control first, on a network where the robot has internet access and your computer can reach the robot. An iPhone Personal Hotspot did not allow the second part in our tests.

### 2. Register the app with Microsoft (one time, UC account)

Open the Microsoft Entra admin center (entra.microsoft.com), then go to **App registrations → New registration**:

- **Name:** `Talk with Reachy`. OneDrive names the upload folder after this.
- **Supported account types:** accounts in this organizational directory only (single tenant).
- **Redirect URI:** platform *Public client/native (mobile & desktop)*, value `http://localhost`. This is only needed for the `--browser` sign-in fallback.

After registering:

- On **Overview**, copy the *Application (client) ID* and the *Directory (tenant) ID*.
- Under **Authentication**, set **Allow public client flows** to **Yes**. This is required for device-code sign-in.
- Under **API permissions**, choose **Add a permission → Microsoft Graph → Delegated permissions**, add **`Files.ReadWrite.AppFolder`**, and remove anything else you do not need.

`Files.ReadWrite.AppFolder` lets the robot write only to `OneDrive/Apps/Talk with Reachy/`. If the robot is lost, the stored sign-in cannot read or change anything else in the account.

If UC does not let you create app registrations or consent to the permission yourself, send UC IT exactly that request: a single-tenant public-client registration with the delegated Microsoft Graph permission `Files.ReadWrite.AppFolder` and public client flows enabled.

Then edit `src/talk_with_reachy/onedrive_upload.py`:

```python
CLIENT_ID = "<Application (client) ID>"
TENANT = "<Directory (tenant) ID>"
```

Neither value is a secret.

### 3. Publish as a private Hugging Face Space

The easy way is one command in Terminal on a Mac:

```bash
bash deploy/publish_to_hf.sh
```

It sets up the Hugging Face tools inside `deploy/.deploy-venv` (nothing is installed system-wide), opens your browser to sign in to Hugging Face if needed, creates the private Space `<your-account>/talk_with_reachy`, and uploads the app. It stops if a Space with that name already exists and is public. Run it again after any code change to update the Space.

To do it by hand instead, create a new Space with SDK **Static** and visibility **Private**, then push this repository to it:

```bash
git lfs install                      # the avatars and images are stored with Git LFS
git remote add space https://huggingface.co/spaces/<your-account>/talk_with_reachy
git push space talk-with-reachy:main
```

Keep the `reachy_mini_python_app` tag in the YAML header of `README.md`. The Control App finds apps by that tag.

The two GitHub workflows that sync to Hugging Face (`sync-hf-space.yml`, `pr-hf-space-preview.yml`) still point at Pollen's Spaces. Delete them, or change the repository IDs if you want GitHub to publish for you.

### 4. Install on the robot

In Reachy Mini Control, sign in to Hugging Face with an account that can see the private Space. Open the app store and search for *Talk with Reachy*. The app appears with a **Private** badge; the store also has a *Private* filter. Install it like any other app.

### 5. Sign in to OneDrive (one time)

The easiest path signs in on a Mac and then copies the sign-in to the robot.

**a. On the Mac, no robot needed.** Run:

```bash
bash deploy/onedrive_dry_run.sh
```

A browser opens for the UC sign-in. Use the UC account whose OneDrive should receive the files. The script then writes `connection_check.txt` to the OneDrive app folder, logs a two-line test conversation with the app's own code, and uploads it. It ends with "Everything works" and the name of the test file under **Apps → Talk with Reachy → transcripts**. You can delete that file afterwards.

This checks the whole Microsoft side (UC's sign-in policy, the permission, and the upload) before a robot is involved.

**b. When the robot is on the same network as the Mac.** Run:

```bash
bash deploy/copy_login_to_robot.sh            # or: ... copy_login_to_robot.sh <robot-ip>
```

SSH asks once for the robot's password. The sign-in lands in `/home/pollen/.config/talk_with_reachy/onedrive_token_cache.json`, readable only by the `pollen` user. A running app picks it up on its next upload pass; no restart is needed.

**Alternative: sign in on the robot itself.** Over SSH, run `/venvs/apps_venv/bin/talk-with-reachy-onedrive-login`. It prints a code and a Microsoft URL; open the URL on any phone or laptop, enter the code, and sign in. Some university tenants block this device-code sign-in; the Mac path above avoids it.

### 6. Check that it works

Start the app, say a few sentences to Reachy, and wait up to five minutes. A file should appear in OneDrive under **Apps → Talk with Reachy → transcripts**. On the robot, `ls ~/talk_with_reachy_data/transcripts` shows the local copies.

## Behavior to know about

- **Upload timing.** Files upload every five minutes while the app runs, and once more when it stops. A file that is still growing is re-uploaded and replaced in OneDrive.
- **Nothing is lost offline.** If the robot has no internet, is signed out, or loses power, the files stay on the robot. They upload on the next successful pass, including after the next app start. Local files are never deleted by the app.
- **Sign-in can expire,** for example after a long period without use or when UC's sign-in policy requires it. The app then logs a warning and keeps writing locally; run the login command again.
- **Everyone who talks to Reachy is logged,** not only consented participants. The app has no way to tell who is speaking.
- **Shutdown.** When the app is stopped, it waits up to 8 seconds for the final upload, because the daemon terminates apps that take longer than 20 seconds to stop. Anything not uploaded then goes up on the next start.

## Settings

All settings are optional environment variables. You can put them in the app's `.env` file.

| Variable | Default | Purpose |
|---|---|---|
| `TALK_WITH_REACHY_LOGGING` | `1` | Set to `0` to turn transcript logging off, for example while developing in simulation. |
| `TALK_WITH_REACHY_DATA_DIR` | `~/talk_with_reachy_data` | Where transcript files are written. |
| `TALK_WITH_REACHY_ROBOT_ID` | host name | First part of each file name; use it to tell robots apart. |
| `TALK_WITH_REACHY_ONEDRIVE_CLIENT_ID` | `CLIENT_ID` in `onedrive_upload.py` | Overrides the client ID. Upload is off when neither is set. |
| `TALK_WITH_REACHY_ONEDRIVE_TENANT` | `TENANT` in `onedrive_upload.py` | Overrides the tenant. |
| `TALK_WITH_REACHY_UPLOAD_INTERVAL_S` | `300` | Seconds between upload passes. |

## Where the changes are

| File | Change |
|---|---|
| `src/talk_with_reachy/study_log.py` | New. Writes the transcript files. |
| `src/talk_with_reachy/onedrive_upload.py` | New. Microsoft sign-in, the background uploader, and the login command. |
| `src/talk_with_reachy/console.py` | One line in `_dispatch_transcript` passes each transcript to `study_log`. |
| `src/talk_with_reachy/main.py` | `study_log.start()` before the conversation starts and `study_log.stop()` when it ends. |
| `tests/test_study_log.py`, `tests/test_onedrive_upload.py` | New tests. Microsoft Graph is mocked. |
| Everything else | Package rename only (`reachy_mini_conversation_app` → `talk_with_reachy`). |

In the upstream README, the command `reachy-mini-conversation-app` is `talk-with-reachy` in this fork.
