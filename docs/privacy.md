# Privacy policy

Last updated 31 July 2026.

This policy covers the automated Instagram assistant behind
[@thenightlybuild](https://www.instagram.com/thenightlybuild/), which replies to
comments and direct messages on that account. The software is open source and
you can read exactly what it does in
[this repository](https://github.com/mortennordbye/reelsmith), specifically the
`gateway/` directory.

Contact: **thenightlybuild@nordbye.it**

## What this service does

When you comment a keyword such as "SEND" on one of the account's Reels, the
service sends you one automated private reply offering a link. If you then
message the account, it checks whether you follow it and, if you do, sends the
link. That is the whole feature.

**Replies are automated, not written by a person at the time you receive them.**

## What it stores

Only what is needed to avoid messaging you twice and to know where you are in
that exchange:

| Data | Why |
|---|---|
| Your Instagram-scoped user ID | To send you the reply you asked for and to recognise you if you write again. This is an ID scoped to this app, not your Instagram username or account ID. |
| The ID of the comment you left | So you are never sent the same automated reply twice. Instagram permits exactly one private reply per comment. |
| Whether you follow the account | Read at the moment you message, because the link is offered to followers. |
| Timestamps | When you last messaged, and whether the link was sent. |

## What it does not store

- **The content of your messages.** Inbound message text is never written to
  storage. The service reacts to the fact that you wrote, not to what you said.
- **The text of your comments.** Comments are read to check for the keyword and
  are not retained.
- Your email address, phone number, real name, location, or any profile field
  beyond your username and follower relationship.

There is no analytics, no advertising, no profiling, and no tracking across
sites. Nothing is sold or shared with anyone.

## Who it is shared with

Nobody. The only third party involved is Meta, because the messages travel over
the Instagram Platform API and Meta necessarily processes them as the operator
of Instagram. Meta's handling of your data is governed by
[Meta's privacy policy](https://www.instagram.com/legal/privacy/).

The service runs on hardware operated privately by the account owner, not on a
third-party cloud, and the data described above never leaves it.

## How long it is kept

Conversation records are kept while the account operates, so that you are not
re-sent an automated message you already received. Ask for deletion at any time
and it is removed.

## Deleting your data

Email **thenightlybuild@nordbye.it** from any address, or send a direct message
to the account, saying you want your data deleted. Include your Instagram
handle so the record can be found. It will be deleted within 30 days, usually
sooner.

Blocking the account, or deleting your comment, also stops any further automated
message. It does not by itself erase the stored ID, so ask if you want that gone.

## Your rights

If you are in the EEA or the UK, the GDPR applies. You may request access to
what is held about you, correction, deletion, or a copy in a portable form, and
you may object to the processing. Use the contact address above. The legal basis
is legitimate interest in answering a message you initiated, and you can end it
at any time by asking.

## Children

The account is aimed at professional software developers, and the service is not
directed at anyone under 13.

## Changes

Material changes will be published here with a new date at the top. This
document is version controlled, so its full history is public in the repository.
