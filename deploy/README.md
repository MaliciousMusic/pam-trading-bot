# Deploying to Oracle Cloud Free Tier

Goal: the bot keeps running 24/7 on a free always-on VM, you access the UI
over SSH tunnel from your laptop. No paid APIs, no paid hosting.

## 1 · Sign up for Oracle Cloud

1. Go to <https://www.oracle.com/cloud/free/> and create an Always Free account.
   You need a credit card for identity verification — Oracle does not charge
   it as long as you only use Always Free resources.
2. Pick a home region close to you (you cannot change this later).
3. Wait for the account to finish provisioning (can take 5–30 min).

## 2 · Generate an SSH key on your laptop

On Windows (PowerShell or Git Bash):

    ssh-keygen -t ed25519 -f $HOME/.ssh/pam_oracle

Accept the default passphrase (empty) or set one. This creates
`~/.ssh/pam_oracle` (private) and `~/.ssh/pam_oracle.pub` (public).
You'll paste the `.pub` file contents into Oracle in the next step.

## 3 · Create the VM instance

1. Oracle Cloud Console → **Compute → Instances → Create Instance**.
2. **Image**: "Canonical Ubuntu 22.04" (or 24.04).
3. **Shape**: click Change shape → **Ampere / VM.Standard.A1.Flex**.
   - OCPUs: `2`  (you can use up to 4 free)
   - Memory: `12 GB`  (up to 24 free)
   - This is the Always Free ARM shape. If you see "out of capacity", retry
     later or pick a different availability domain — Oracle sometimes rations
     A1 shape in popular regions.
4. **Networking**: leave defaults ("Create new virtual cloud network",
   "Assign a public IPv4 address": yes).
5. **SSH keys**: paste the contents of `~/.ssh/pam_oracle.pub`.
6. Click **Create**. Wait 1–2 min for it to reach "Running" state.
7. Copy the **Public IP address** shown on the instance detail page.

## 4 · First SSH into the instance

From your laptop:

    ssh -i ~/.ssh/pam_oracle ubuntu@<public-ip>

You should land on the Ubuntu prompt. If SSH hangs, see "Firewall" below.

## 5 · Run the installer

Still in the SSH session:

    curl -fsSL https://raw.githubusercontent.com/MaliciousMusic/pam-trading-bot/main/deploy/install.sh | sudo bash

This installs Python, clones the repo, creates a venv, writes a systemd
unit, and starts the bot. The last lines of output will show the service
status (should be `active (running)`).

Watch live logs:

    journalctl -u pam-bot -f

You should see the same tick lines you saw running locally.

## 6 · Access the UI

Keep the bot bound to `127.0.0.1` on the VM (safer — nobody on the internet
can hit it). From your laptop, open a tunnel:

    ssh -i ~/.ssh/pam_oracle -L 8765:localhost:8765 ubuntu@<public-ip>

Then open <http://localhost:8765> on your laptop. Closing the SSH session
closes the tunnel but the bot keeps running on the VM.

## 7 · Updating after a new commit to main

SSH in and re-run the installer — it's idempotent:

    curl -fsSL https://raw.githubusercontent.com/MaliciousMusic/pam-trading-bot/main/deploy/install.sh | sudo bash

That pulls `origin/main`, reinstalls deps if changed, and restarts the service.

## Firewall notes

Oracle's default Ubuntu image ships with `iptables` rules that **drop all
inbound traffic except SSH (port 22)**. That's what we want — the bot's
port 8765 stays invisible to the internet, and you reach it only via SSH
tunnel.

If you want to expose the UI publicly (NOT recommended without auth),
you would need to:

1. Oracle Console → your VCN → Security List → add an ingress rule for
   TCP 8765 from `0.0.0.0/0`.
2. On the VM: `sudo iptables -I INPUT 6 -p tcp --dport 8765 -j ACCEPT`
   and persist with `sudo netfilter-persistent save`.
3. Edit `config/settings.yaml` → `ui.host: 0.0.0.0` and restart the service.

I'd hold off on this until you've added HTTP auth in front of the UI.

## Troubleshooting

- **`Active: failed` or crash loop**: `journalctl -u pam-bot -n 100` to see
  the traceback.
- **`ModuleNotFoundError`**: the venv didn't install cleanly —
  `sudo -u pam /home/pam/pam-trading-bot/.venv/bin/pip install -r /home/pam/pam-trading-bot/requirements.txt`.
- **SSH tunnel refuses connection**: check the bot is listening —
  `ss -ltn | grep 8765` on the VM. Should show `127.0.0.1:8765`.
- **Instance "out of capacity"** on create: Oracle is rationing A1 shape.
  Try a different availability domain in the same region, or retry later.
