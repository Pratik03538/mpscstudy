# Oracle Cloud Always Free deployment (bot + Mini App)

This guide runs the existing Telegram bot and the FastAPI Mini App on **one Ubuntu VM**. Both processes use the same local SQLite question database and the same persistent state database.

## Free-tier sizing and limits

For OCI Always Free, the Ampere A1 allowance is **1,500 OCPU-hours and 9,000 GB-hours per month**, equivalent to one 2-OCPU / 12-GB VM running for a full 744-hour month. The combined Always Free boot + block volume allowance is 200 GB; keep the default 50-GB boot volume to stay within the allowance. Always Free compute must be created in the tenancy's home region. See the official [Always Free resource list](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm).

**Important:** Always Free instances can be reclaimed if, over a 7-day period, CPU 95th-percentile utilization, network utilization, and (for A1) memory utilization are all below 20%. This is free-tier hosting, not a guaranteed production SLA. OCI may also show temporary "out of host capacity" errors in a home region.

## 1. Create the Oracle account and VM

1. Open [Oracle Cloud Free Tier](https://www.oracle.com/cloud/free/) and create one account. Sign-up may request a supported credit/debit card for identity verification. Do not upgrade the account or create resources marked paid.
2. Select the closest available Indian home region during account registration. Always Free compute must be provisioned in this home region.
3. Open **Compute → Instances → Create instance**.
4. Name it `mpsc-arena`.
5. Choose **Ubuntu 24.04 LTS** (standard Ubuntu image, ARM-compatible; avoid Minimal Ubuntu for this ARM setup).
6. Select shape **VM.Standard.A1.Flex**. Set **2 OCPUs** and **12 GB memory** total. If capacity is unavailable, retry later or try another availability domain in the same home region. Do not choose a paid shape.
7. Keep the default boot volume at **50 GB**.
8. For networking, create/use a VCN with an **Internet Gateway** and a **public subnet**, and assign a public IPv4 address.
9. Generate or upload an SSH key pair. Download and securely keep the private key. Oracle does not retain a copy of a generated private key.
10. Create the instance and note its public IP address.

Ubuntu 24.04 is listed by OCI as an image supported on Arm-based shapes. OCI's Ubuntu SSH username is `ubuntu`.

## 2. Open only the required network ports

In the VCN security list or the instance's Network Security Group, add these **stateful ingress** rules:

| Source | Protocol | Destination port | Purpose |
|---|---|---:|---|
| Your public IP followed by `/32` | TCP | 22 | SSH administration |
| `0.0.0.0/0` | TCP | 80 | Let's Encrypt validation / HTTP redirect |
| `0.0.0.0/0` | TCP | 443 | HTTPS Mini App |

Do **not** expose port 8000 publicly; Uvicorn listens only on `127.0.0.1` and Nginx proxies HTTPS traffic to it. If your home IP changes, update the SSH ingress rule. The install script also configures Ubuntu UFW to allow SSH and Nginx's HTTP/HTTPS ports.

## 3. Connect from Windows PowerShell

Use the private key downloaded during VM creation. Replace the key path and IP:

```powershell
ssh -i "$env:USERPROFILE\Downloads\oracle_ssh.key" ubuntu@YOUR_PUBLIC_IP
```

For an Ubuntu image, the login user is `ubuntu`. Keep the private key on your own PC; never send it in chat or commit it to GitHub.

## 4. Install the application

In the SSH terminal on the VM, run:

```bash
git clone https://github.com/Pratik03538/mpscstudy.git /tmp/mpscstudy-bootstrap
bash /tmp/mpscstudy-bootstrap/telegram_bot/deploy/oracle/install.sh
```

The script installs Python, Nginx, Certbot and app dependencies; clones the current `main` branch into `/opt/mpscstudy`; creates a virtual environment; and installs the systemd service files. It deliberately does **not** start the bot until the question database and environment file are ready.

## 5. Upload the existing question database from Windows

Open a **second** PowerShell window on your own PC (not the SSH terminal). From the local repository:

```powershell
cd C:\Users\Pratik\PycharmProjects\mpsckatta
scp -i "$env:USERPROFILE\Downloads\oracle_ssh.key" ".\telegram_bot\database\mpsc_subject_only.db" ubuntu@YOUR_PUBLIC_IP:/home/ubuntu/mpsc_subject_only.db
```

Then return to the SSH terminal and install the uploaded file in its permanent location:

```bash
sudo install -o ubuntu -g ubuntu -m 0640 \
  /home/ubuntu/mpsc_subject_only.db \
  /opt/mpscstudy/telegram_bot/database/mpsc_subject_only.db
rm /home/ubuntu/mpsc_subject_only.db
```

This database is not stored in GitHub; the copy from your existing Windows project is required. Check that its size is non-zero:

```bash
ls -lh /opt/mpscstudy/telegram_bot/database/mpsc_subject_only.db
```

## 6. Configure the bot safely

Edit the root-owned environment file on the VM:

```bash
sudo nano /etc/mpsc-study.env
```

Copy the actual values from your existing local `telegram_bot/.env`, but make sure the database paths are the following absolute paths:

```env
BOT_DB_PATH=/opt/mpscstudy/telegram_bot/database/mpsc_subject_only.db
BOT_STATE_DB_PATH=/var/lib/mpsc-study/bot_progress.sqlite3
```

Also set your real `TELEGRAM_BOT_TOKEN`, channel ID/link, discussion-group ID/link, admin IDs, and existing event settings. Leave these blank until HTTPS and BotFather setup is complete:

```env
MINI_APP_URL=
MINI_APP_SHORT_NAME=
```

Save the file, then lock down its permissions:

```bash
sudo chown root:root /etc/mpsc-study.env
sudo chmod 600 /etc/mpsc-study.env
```

The token belongs only in this server-side file. Never commit the real `.env` to GitHub or paste the token into chat.

## 7. Start both processes and test the API locally

```bash
sudo systemctl enable --now mpsc-bot mpsc-miniapp
sudo systemctl status mpsc-bot --no-pager
sudo systemctl status mpsc-miniapp --no-pager
curl http://127.0.0.1:8000/health
```

Expected API response:

```json
{"ok":true,"service":"mpsc-arena-miniapp"}
```

To inspect logs:

```bash
sudo journalctl -u mpsc-bot -n 100 --no-pager
sudo journalctl -u mpsc-miniapp -n 100 --no-pager
```

The bot log should report that it loaded subjects and MCQs. Both services must stay on this **same VM** because they share a SQLite state file.

## 8. Add a free HTTPS hostname

Telegram Mini Apps require HTTPS. If you do not own a domain, one option is a free DuckDNS subdomain:

1. Open [DuckDNS](https://www.duckdns.org/), sign in and create a subdomain.
2. Set its IPv4 address to the VM's public IP.
3. Wait until the hostname resolves to that IP.
4. In the SSH terminal, install the Nginx site config and replace `YOUR_DOMAIN`:

```bash
sudo cp /opt/mpscstudy/telegram_bot/deploy/oracle/nginx-mpsc-arena.conf /etc/nginx/sites-available/mpsc-arena
sudo nano /etc/nginx/sites-available/mpsc-arena
sudo ln -s /etc/nginx/sites-available/mpsc-arena /etc/nginx/sites-enabled/mpsc-arena
sudo nginx -t
sudo systemctl enable --now nginx
```

5. Obtain and install a free Let's Encrypt certificate:

```bash
sudo certbot --nginx -d YOUR_DOMAIN
```

Follow Certbot's HTTPS redirect prompt. Verify:

```bash
curl https://YOUR_DOMAIN/health
```

## 9. Connect the Telegram Mini App

1. Open `@BotFather` → `/mybots` → your bot → **Bot Settings → Configure Mini App** and create/configure a named Mini App.
2. Set the Mini App URL to `https://YOUR_DOMAIN` and note its exact short name.
3. Edit the server file:

```bash
sudo nano /etc/mpsc-study.env
```

Set:

```env
MINI_APP_URL=https://YOUR_DOMAIN
MINI_APP_SHORT_NAME=YOUR_EXACT_BOTFATHER_SHORT_NAME
```

4. Restart the processes:

```bash
sudo systemctl restart mpsc-bot mpsc-miniapp
```

Open the bot dashboard and test the Mini App from Telegram. In a battle, both players must tap the **Open Battle Room** link; Telegram does not let the bot force-open the Mini App on another person's device.

## 10. Updating the code later

When a new version is committed to GitHub:

```bash
git -C /opt/mpscstudy pull --ff-only origin main
/opt/mpscstudy/.venv/bin/python -m pip install -r /opt/mpscstudy/telegram_bot/requirements.txt
/opt/mpscstudy/.venv/bin/python -m pip install -r /opt/mpscstudy/telegram_bot/miniapp/requirements.txt
sudo systemctl restart mpsc-bot mpsc-miniapp
```

Do not delete `/var/lib/mpsc-study/bot_progress.sqlite3` or the question database; they contain persistent user, event and battle data. Back them up before making database changes.

## Troubleshooting

- `out of host capacity`: try another availability domain in the home region or try again later.
- SSH timeout: verify the VM public IP, public subnet / Internet Gateway, and TCP 22 ingress rule.
- `Question DB not found`: verify `BOT_DB_PATH`, uploaded filename, and file permissions.
- Mini App returns 503: verify `BOT_STATE_DB_PATH`, then inspect `journalctl -u mpsc-miniapp`.
- Telegram rejects the Mini App URL: verify HTTPS certificate, BotFather URL/domain configuration, and exact short name.
- If the free-tier VM is reclaimed for low utilization, the service may be unavailable until you can restart/recreate it. Oracle's Always Free idle-reclamation rules apply; this setup does not guarantee an SLA.
