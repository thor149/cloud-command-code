# Cloud Command Code V1

A private Telegram client for read-only Command Code research on one or more repositories. It uses Telegram long polling, so the VM needs outbound internet access but no public bot endpoint. `/plan`, `/task`, and plain text questions all run Command Code with the fixed `--permission-mode plan` flag. The bot does not offer file edits, shell execution, commits, or deployment.

## What you need

- An Ubuntu 24.04 VM with outbound internet access. An Oracle Always Free eligible instance is one option; confirm eligibility and capacity in your tenancy before creating it.
- Python 3.11 or newer and Node.js 22 or newer.
- A Telegram bot token from [@BotFather](https://t.me/BotFather).
- A repository cloned on the VM, plus a working Command Code login for the same Linux account that runs this service.

## Create the Oracle VM

In Oracle Cloud, choose **Compute → Instances → Create instance** in your tenancy's home region. Select an **Always Free eligible** Ubuntu 24.04 image and the **VM.Standard.A1.Flex** shape. For one VM, set **2 OCPUs and 12 GB RAM** if those Always Free resources are available in your tenancy. Choose a public subnet, assign a public IPv4 address for SSH, and download the SSH private key. You do not need to open an inbound HTTP port for this bot. Oracle documents [the A1 monthly allowance and possible capacity shortages](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm); check the final cost estimate before you click Create. Keep the private key off the VM and out of this project.

From Windows PowerShell, connect with `ssh -i "C:\path\to\oracle-key.key" ubuntu@YOUR_SERVER_IP`. If SSH reports that the key permissions are too open, restrict access to your Windows account before retrying. Oracle's [Linux instance tutorial](https://docs.oracle.com/en-us/iaas/Content/Compute/tutorials/first-linux-instance/overview.htm) covers the console networking and SSH key steps.

## Set up Ubuntu

SSH into the VM as `ubuntu`. Install the base tools:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip curl unzip
python3 --version
```

Install Node.js 22 or newer using the current [Node.js download instructions](https://nodejs.org/en/download) for Linux, then verify `node --version` and `npm --version`. Install Command Code using its [official quickstart](https://commandcode.ai/docs/quickstart):

```bash
sudo npm i -g command-code@latest
cmd --version
```

For this server, the easiest authentication path is a Command Code API key. Generate one in [Command Code Studio](https://commandcode.ai/docs/studio), then add `COMMAND_CODE_API_KEY=...` to the private `.env` file below. Command Code reads this variable directly in headless runs, so a browser login on the VM is unnecessary. The service passes this key to Command Code but removes the Telegram credentials from its child process environment. An interactive `cmd login` as `ubuntu` is also possible if you can complete its browser flow.

Clone a project, for example:

```bash
mkdir -p /home/ubuntu/workspace
git clone YOUR_REPOSITORY_URL /home/ubuntu/workspace/hirable
```

If the project is private, configure a read-only Git credential on the VM first. The bot never needs a Git write credential.

Copy the provided `commandcode-bot-v1.zip` to the VM, or clone this project after putting it in a Git repository. From Windows PowerShell, an example transfer is:

```powershell
scp -i "C:\path\to\oracle-key.key" "C:\Users\Lenovo\OneDrive\Desktop\CloudCommandCode\commandcode-bot-v1.zip" ubuntu@YOUR_SERVER_IP:/home/ubuntu/
```

On the VM, unpack and install it:

```bash
mkdir -p /home/ubuntu/commandcode-bot
unzip /home/ubuntu/commandcode-bot-v1.zip -d /home/ubuntu/commandcode-bot
```

Then:

```bash
cd /home/ubuntu/commandcode-bot
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp projects.example.json projects.json
cp .env.example .env
chmod 600 .env projects.json
```

Edit `projects.json` to list only the local repositories the bot may inspect. Paths must be absolute and already exist. Edit `.env` with the token, your numeric Telegram user ID, and the correct `PROJECTS_FILE` path. Use `command -v cmd` to find the Linux Command Code executable and set `COMMAND_CODE_BIN` to that absolute path. Do not commit either file.

If you use a Command Code API key, add an uncommented `COMMAND_CODE_API_KEY=...` line to `.env`. After loading that file in the next step, verify Command Code separately from the bot:

```bash
set -a
. ./.env
set +a
cd /home/ubuntu/workspace/hirable
cmd -p "Explain this repository" --permission-mode plan --skip-onboarding --trust
cd /home/ubuntu/commandcode-bot
```

To find your Telegram user ID without putting the token in a URL or command history, message your new bot privately, then run **before starting the service**:

```bash
.venv/bin/python -m commandcode_bot.identify
```

Paste the BotFather token at the hidden prompt. If no message appears, send the bot another message and retry. The ID shown for your account is `TELEGRAM_USER_ID`.

Test the bot interactively:

```bash
set -a
. ./.env
set +a
.venv/bin/python -m commandcode_bot.bot
```

In Telegram, send `/start`, `/projects`, `/use hirable`, `/plan explain the voice interview flow`, and `/task identify likely causes of a hanging interview`. Stop the foreground process before enabling the service: only one poller may use a bot token at a time.

## Keep it running with systemd

The included unit assumes the paths above and the Linux user `ubuntu`. Adjust it if your setup differs.

```bash
sudo cp deploy/commandcode-bot.service /etc/systemd/system/commandcode-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now commandcode-bot
sudo systemctl status commandcode-bot
journalctl -u commandcode-bot -n 100 --no-pager
```

The `.env` file is read by systemd. After interactive testing, make it root-owned and mode `600` so the Command Code process cannot read it directly: `sudo chown root:root .env && sudo chmod 600 .env`. Keep it out of Git. The controller removes `TELEGRAM_*` values from the Command Code child process environment. Do not paste the bot token into the unit file or a chat. If a token leaks, rotate it with BotFather. The bot ignores messages from all other users and from groups, including a group where your own account is present.

## Operating limits

- Only one Command Code run executes at a time. `/status` stays responsive while a run is active.
- A run times out after 15 minutes by default; change `RUN_TIMEOUT_SECONDS` in `.env` within 30–3600 seconds.
- Output over 7000 characters is sent as a text document. Output over 64 KiB is explicitly truncated.
- The selected project is held in memory. After a service restart, send `/use <project>` again. Sessions are not resumed in V1.
- Telegram messages and returned answers pass through Telegram. Avoid asking the bot to expose secrets or private candidate data.
- Plan mode is a Command Code tool permission boundary. For an additional operating system boundary, run the bot under a dedicated user that has read access only to the approved project checkouts.

## Local verification

With Python 3.11+:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

On Windows, replace `.venv/bin/python` with `.venv/Scripts/python.exe`. The Command Code short alias on native Windows is `cmdc`; `cmd` there is the Windows shell. The production target in this guide is Ubuntu.

CLI flags and read-only behavior are based on [Command Code headless mode](https://commandcode.ai/docs/headless). Polling behavior is based on the [Telegram Bot API](https://core.telegram.org/bots/api#getupdates) and [python-telegram-bot](https://docs.python-telegram-bot.org/en/stable/telegram.ext.application.html).
