# lurkme

Twitch chat lurker bot. It joins your pinned channels and live followed channels,
fills up to 80 channels with the top live streams, and logs any subs gifted to you.
The channel list is refreshed every 30 minutes, and channels that drop out are left.

Being in chat earns third-party bot points (StreamElements, Nightbot, etc.).
It does **not** earn official Channel Points or watch time, because those need the video player open.

## Setup

1. **Create a Twitch app** at <https://dev.twitch.tv/console/apps>:
   - OAuth Redirect URL: `http://localhost:3000`
   - Client Type: **Confidential**
   - Copy the **Client ID** and generate a **Client Secret**.

2. **Generate a user token with that same app** using the [Twitch CLI](https://dev.twitch.tv/docs/cli/):

   ```sh
   twitch configure                                 # enter the Client ID and Secret from step 1
   twitch token -u -s "chat:read user:read:follows"
   ```

   This prints a *User Access Token* and a *Refresh Token*.

   > The bot can only refresh tokens issued by **your** app. A token from a third-party
   > generator such as twitchtokengenerator.com belongs to that site's app. The bot
   > can't renew it, so it will stop when the token expires, usually within a few hours.

3. **Enter them.** On a VPS, the installer asks for them (see [Run it on a VPS](#run-it-on-a-vps)).
   To run it on your own computer, copy `.env.example` to `.env` and fill it in:

   | Variable        | Value                                 |
   |-----------------|---------------------------------------|
   | `CLIENT_ID`     | Client ID from step 1                 |
   | `CLIENT_SECRET` | Client Secret from step 1             |
   | `OAUTH_TOKEN`   | User Access Token from step 2         |
   | `REFRESH_TOKEN` | Refresh Token from step 2             |

4. **Run it:**

   ```sh
   pip install -r requirements.txt
   python lurker_bot.py
   ```

Never commit real credentials. `.env` is gitignored.

## Optional settings

Set these the same way as the variables above. Each takes a comma-separated list.

| Variable           | Default | What it does |
|--------------------|---------|--------------|
| `CHANNELS`         | none    | Channels to always join, live or not, e.g. `streamer1, streamer2`. They get priority and count toward the 80. |
| `STREAM_LANGUAGES` | `en`    | Languages for the top-streams fill, as two-letter codes, e.g. `en, es`. Use `any` for all languages. |
| `CATEGORIES`       | all     | Only fill from these categories, e.g. `Just Chatting, Fortnite`. Use the exact name shown on Twitch. |

Channels are picked in this order: `CHANNELS`, then your live followed channels, then top streams, up to 80 in total.

### Discord gift alerts

| Variable              | What it does |
|-----------------------|--------------|
| `DISCORD_WEBHOOK_URL` | Posts a card to this Discord webhook whenever someone gifts you a sub: the channel and its avatar, who gifted it, the tier and the length. Create one under Server Settings → Integrations → Webhooks. Keep it private, because anyone with the URL can post to that channel. |
| `DISCORD_USER_ID`     | Pings you in each alert. This must be your numeric user ID, because a webhook can't ping by username. To find it, turn on Settings → Advanced → Developer Mode, then right-click your name → Copy User ID. |

Alerts are sent in the background and retried if Discord is down or rate-limited. If the bot restarts, unsent alerts carry over to the next run.

## Run it on a VPS

On any Linux VPS with systemd (Ubuntu, Debian, Fedora, etc.), connect over SSH and run:

```sh
git clone https://github.com/xOVHx/lurkme.git
sudo bash lurkme/deploy/install.sh
```

The installer:

- installs git and Python if they're missing
- puts the code in `/opt/lurkme`, run by a locked-down `lurkme` system user
- asks for your Twitch credentials and settings, and stores them in `/etc/lurkme/lurkme.env`, readable only by root and the bot. Generate the credentials on your own computer first (steps 1–2 above). Secret values stay hidden while you paste them.
- starts the bot as a service that runs on boot and restarts itself after crashes

To deploy a branch other than `main`, clone it with `git clone -b <branch> ...`.

**Automated deploys** (no terminal, e.g. a script or an AI agent with SSH access): before running the installer, create
`/etc/lurkme/lurkme.env` in the same format as `.env.example`. The installer then uses it without prompting and fixes its
permissions. Alternatively, pass the values as environment variables:
`sudo CLIENT_ID=… CLIENT_SECRET=… OAUTH_TOKEN=… REFRESH_TOKEN=… bash lurkme/deploy/install.sh`.
Avoid that form on shared machines, because it puts the secrets in your shell history.

| Task                          | Command |
|-------------------------------|---------|
| Watch the log                 | `journalctl -u lurkme -f` |
| Check it's running            | `systemctl status lurkme` |
| Update to the latest code     | `sudo bash /opt/lurkme/deploy/install.sh` |
| Enter new tokens / settings   | `sudo bash /opt/lurkme/deploy/install.sh --reconfigure` |
| Remove everything             | `sudo bash /opt/lurkme/deploy/install.sh --uninstall` |

Run the bot in only one place at a time. Two copies logged in as the same account would double the join rate and could hit Twitch's limit.

## Keeping it running

- Validates the token hourly and refreshes it before it expires.
- Rejoins all channels after Twitch reconnects.
- Pings Twitch when chat is quiet, and restarts itself if the connection stalls.
- Retries Twitch outages and API errors with backoff (5s up to 5 min). It keeps its current channels in the meantime.
- Exits only when the token is dead and can't be renewed. That happens if you changed your password, disconnected the app, or used a third-party token. On a VPS the service then stays stopped instead of restarting in a loop (`systemctl status lurkme` shows `status=78/CONFIG`). Generate a new token (step 2) and run `sudo bash /opt/lurkme/deploy/install.sh --reconfigure`.

## Rate limits

JOINs are paced at about 17 per 10 seconds, under Twitch's limit of 20.
The bot never sends chat messages.
It makes a few API calls every 30 minutes, far below Twitch's API limits.
