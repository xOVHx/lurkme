# lurkme

Twitch chat lurker bot. It joins your live followed channels, fills up to 80
channels with the top live English streams, and logs any subs gifted to you.
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

3. **Set the environment variables.** On Railway, add them under the service's **Variables**.
   For local runs, copy `.env.example` to `.env` and fill it in:

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

## Keeping it running

- Validates the token hourly and refreshes it before it expires.
- Rejoins all channels after Twitch reconnects.
- Pings Twitch when chat is quiet, and restarts itself if the connection stalls.
- Retries Twitch outages and API errors with backoff (5s up to 5 min). It keeps its current channels in the meantime.
- Exits only when the token is dead and can't be renewed. That happens if you changed your password, disconnected the app, or used a third-party token. Generate a new token (step 2) and update the variables.

## Rate limits

JOINs are paced at about 17 per 10 seconds, under Twitch's limit of 20.
The bot never sends chat messages.
It makes a few API calls every 30 minutes, far below Twitch's API limits.
