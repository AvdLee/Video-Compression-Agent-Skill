# Platform size limits

The `--target` option and `advise --target` use the values in the table. Each one was checked against the official page
listed on 2026-10-30. Limits change, so treat the date as part of the data. A platform without a verifiable official number
is left out on purpose.

| `--target` name | Size used | What it is | Source |
| --- | --- | --- | --- |
| `discord` | 10 MB | Free upload limit. The File Attachments FAQ says 20 MB since August 2026, but the Account Caps page still lists 10 MB. The skill uses the smaller value so a file works under either. | [File Attachments FAQ](https://support.discord.com/hc/en-us/articles/25444343291031-File-Attachments-FAQ), [Account Caps](https://support.discord.com/hc/en-us/articles/33694251638295-Discord-Account-Caps-Server-Caps-and-More) |
| `whatsapp` | 64 MB | Default video size limit on a slower connection in WhatsApp Web and Desktop (480p). On a faster connection the same page lists 100 MB. Not verified for the phone apps. | [WhatsApp Help Center: send media](https://faq.whatsapp.com/453914586839706/) |
| `email` | 25 MB | Gmail attachment limit for personal accounts. Other providers differ. Work and school accounts are set by the administrator. | [Gmail Help: attachments](https://support.google.com/mail/answer/6584) |
| `slack` | 1000 MB | Maximum file size for members and guests on all plans (1 GB). | [Slack Help: add files](https://slack.com/help/articles/201330736-Add-files-to-Slack) |

## Left out

- iMessage: Apple does not publish a current attachment limit for Messages. Use `--target-mb` with a number you know.
- Instagram, TikTok, X, YouTube and others: limits depend on duration, account type and the app version, and the official pages
  do not give one stable number. Use `--target-mb` with the number from the platform you are posting to.

The WhatsApp Business Platform (the API for businesses) has different media limits, 16 MB for video. Those do not apply to messages
sent from the WhatsApp apps.

Sizes in this skill use 1 MB = 1,000,000 bytes and the encode aims at 95% of the target. If a platform counts in MiB, pass a slightly
smaller `--target-mb`.
