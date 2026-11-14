# first-contact

Postfix milter that warns a recipient when they receive a message **for the
first time** from a given sender, or when a **known sender reappears after a
long absence** (90 days by default, configurable).

The warning is **visible to the user**, not just technical:

* a **banner at the top of the message body** (default), advising caution with
  attachments and links;
* alternatively or in addition, a **subject prefix**;
* always, an `X-First-Contact` header usable in client rules.

```
ATTENZIONE: e' il primo messaggio che ricevi da mario.rossi@example.com.
Non e' detto che sia pericoloso, ma presta attenzione ad allegati e link:
non aprire file e non cliccare collegamenti se non ti fidi del mittente.
```

The default text is Italian; it can be replaced in `[banner] text`.

If the milter fails or is stopped, mail keeps flowing
(`milter_default_action = accept`). With the body banner the original content
stays in place: the banner is inserted into the existing text parts, without
creating new MIME parts and without turning the message into an attachment.
HTML, attachments and encodings remain intact.

Italiano: [README.it.md](README.it.md).

## 1. How it works

```
                 +-----------+   milter    +-------------------+
sender --> smtpd --> rspamd --> opendkim --> first-contact --> queue --> LMTP/LDA
                                                      |
                                                      v
                                        /var/lib/first-contact/contacts.db
                                        table contacts(sender, recipient, ...)
```

* The milter receives from Postfix the **envelope** sender (`MAIL FROM`), the
  recipients (`RCPT TO`), and the message headers and body at the end of data
  (`eom`).
* For every (sender, recipient) pair it runs an `INSERT OR IGNORE` on SQLite:
  if the row is inserted, this is the first message. The operation is atomic,
  so two copies of the same message arriving together do not produce two
  warnings. The same query also detects a sender **absent for too long** (see
  section 5.1): again, only the first of several simultaneous messages raises
  the warning.
* The whitelist is checked **before anything else**: if the sender or the
  client network matches, the message gets no warning and is not written to
  the database.
* When the pair is new it adds one header for **each new recipient** (a
  message with three new recipients gets three headers, one per recipient)
  and, if configured, the visible warning (banner and/or subject).
* Every error (locked database, exception, socket, oversized body) is logged
  and swallowed: the milter always answers `CONTINUE`, mail never stops. If
  the visible warning cannot be applied, the header remains.

Why a milter and not `header_checks`: `header_checks` does not know the state
of each sender/recipient pair and cannot query a database. A `content_filter`
would mean injecting the message a second time and losing envelope
information. Selectively modifying the body requires a milter.

### How the banner is added

The warning **does not create new MIME parts** and does not change the
message `Content-Type`: it is inserted at the top of the text parts that
already exist.

```
text/plain                  <- warning, then the original text
text/html                   <- yellow box right after <body>, then the original HTML
multipart/alternative       <- the warning goes into both versions
multipart/mixed             <- the text gets the warning, attachments stay attachments
```

For each modified part only the transfer encoding is decoded (`base64`,
`quoted-printable`, `7bit`/`8bit`), the text is added and the same encoding is
reapplied. Headers, boundaries and structure stay as they were: the client
shows a normal, readable message, with no added attachments and no embedded
`.eml`. Attachments (`Content-Disposition: attachment`) and forwarded messages
(`message/rfc822`) are left untouched. This is the same principle used by
disclaimer tools such as alterMIME.

`multipart/signed` and `multipart/encrypted` are not rewritten: any change
would invalidate the signature or encryption.

## 2. Project layout

```
src/firstcontact/       Python package (stdlib only + pymilter for "serve")
  config.py             INI configuration load and validation
  policy.py             address normalisation, "what to look at" rules
  headers.py            safe header construction (ASCII, RFC 5322 folding)
  banner.py             warning text and body MIME rewriting
  whitelist.py          excluded senders/networks, with automatic reload
  store.py              SQLite, atomic "first contact"
  engine.py             decision: header, banner, subject prefix
  milter_app.py         pymilter glue (SMTP callbacks)
  cli.py                serve / check / list / stats / prune / doctor / simulate
etc/first-contact.ini   example configuration
etc/whitelist.txt       example whitelist (one entry per line)
etc/tmpfiles.d/         directory permissions at boot
systemd/                service unit
postfix/                main.cf change (notes on chroot, ordering and DKIM)
packaging/              command launcher, systemd drop-in, RPM spec
debian/                 Debian packaging (debhelper, compat 12)
man/                    first-contact(8) manual page
scripts/install.sh      source install in /opt (dry-run by default)
scripts/uninstall.sh    removal
scripts/run-tests.sh    tests
scripts/build-deb.sh    .deb package
scripts/build-rpm.sh    noarch .rpm package
.github/workflows/      package builds and release on tags
tests/                  161 tests (unittest, no dependencies)
examples/sample.eml     sample message
```

## 3. Installation

Requirements: Postfix, Python >= 3.6 and `pymilter` (`python3-milter` on
Debian, `python3-pymilter` on Enterprise Linux, from EPEL). The packages
install the command, the configuration, the systemd unit and the
`tmpfiles.d` rules; they create the `firstcontact` service user and **enable
and start** `first-contact-milter`. They do not modify Postfix: the milter
must be added to `smtpd_milters` (section 3.3).

### 3.1 deb and rpm packages

Debian and Ubuntu (`all` architecture):

```bash
sudo apt-get install -y python3-milter
sudo dpkg -i first-contact_1.0.3_all.deb
```

The `.deb` is compressed with `xz` and does not use `zstd`: it also installs
on Debian 10. On buster (EOL) `python3-milter` must be taken from
`archive.debian.org` (see the note in `scripts/install.sh`).

Enterprise Linux 8, 9 and 10 (noarch; `pymilter` comes from EPEL):

```bash
sudo dnf install -y epel-release
sudo dnf install ./first-contact-1.0.3-1.el8.noarch.rpm
```

Where the code goes:

| package | Python code | command | configuration |
|---|---|---|---|
| deb | `/usr/lib/python3/dist-packages/firstcontact` | `/usr/bin/first-contact` | `/etc/first-contact/config.ini` |
| rpm | `/usr/lib/python3.X/site-packages/firstcontact` | `/usr/bin/first-contact` | `/etc/first-contact/config.ini` |

Configuration files are installed as `conffile` (`%config(noreplace)` in
RPMs): upgrades do not overwrite them. They contain no secrets, so they are
world-readable; the whitelist and the database remain protected by their
directory permissions.

Check after installation:

```bash
first-contact doctor
systemctl status first-contact-milter --no-pager
```

### 3.2 Building the packages

You need `debhelper` (>= 12) and `dh-python` for the deb, `rpm-build` and
`python3-rpm-macros` for the RPM. The scripts use the version declared in
`src/firstcontact/__init__.py` and write to `dist/`.

```bash
./scripts/run-tests.sh
./scripts/build-deb.sh     # dist/first-contact_1.0.3_all.deb
./scripts/build-rpm.sh     # dist/first-contact-1.0.3-1.el*.noarch.rpm
```

The RPM must be built inside the target Enterprise Linux release (bytecode
and `%{?dist}` tags depend on the release):

```bash
podman run --rm -v "$PWD:/src:z" -w /src rockylinux:8 \
  bash -c 'dnf install -y rpm-build python3-rpm-macros && ./scripts/build-rpm.sh'
```

The `.github/workflows/build-packages.yml` workflow does the same in CI:
artifacts on every push and pull request, packages attached to the GitHub
release for `v*` tags (created with `gh release create`, no third-party
actions).

### 3.3 Postfix: adding the milter

The token to append **at the end** of `smtpd_milters` is:

```
unix:/first-contact/milter.sock
```

The Unix socket is **inside** `/var/spool/postfix` because `smtpd` runs
chrooted (`master.cf`, chroot column = `y`): from inside the chroot the path
is `/first-contact/milter.sock`; on the filesystem it is
`/var/spool/postfix/first-contact/milter.sock`. If the socket is moved outside
`/var/spool/postfix`, Postfix no longer sees it.

```bash
cp -a /etc/postfix/main.cf /etc/postfix/main.cf.first-contact.$(date +%F)
postconf -e "smtpd_milters = $(postconf -h smtpd_milters) unix:/first-contact/milter.sock"
postfix check && systemctl reload postfix
```

The milter must come after `rspamd` (rejected messages must not be recorded)
and, if `opendkim` **signs** mail (`Mode s`/`sv`), before `opendkim` (the
local signature then covers the banner as well). Details and checks are in
`postfix/main.cf.snippet`.

### 3.4 Rspamd integration (optional)

first-contact already works next to Rspamd: both are Postfix milters, and the
order in `smtpd_milters` is enough. Keep first-contact after Rspamd so messages
rejected as spam are not recorded:

```
smtpd_milters = inet:127.0.0.1:11332 unix:/first-contact/milter.sock
```

Rspamd's `known_senders` module is the closest native feature: it keeps a
Redis-backed set of senders for configured sender domains (typically freemail
domains) and sets `KNOWN_SENDER` / `UNKNOWN_SENDER`. It is not equivalent to
first-contact: it is global, not per sender/recipient pair, has no
absence-days rule and no visible banner.

If you only need a native Rspamd header for first-time senders,
`milter_headers` can expose that symbol. This is independent of first-contact;
keep using the milter if you need per-recipient precision or the body banner.

`/etc/rspamd/local.d/known_senders.conf`:

```
enabled = true;
domains = "/etc/rspamd/local.d/known_senders_domains.map";
#servers = "127.0.0.1:6379";
```

The map lists one sender domain per line. Redis must be configured for Rspamd.

`/etc/rspamd/local.d/milter_headers.conf`:

```
use = ["first_sender"];

custom {
  first_sender = <<EOD
  return function(task, common_meta)
    if task:has_symbol('UNKNOWN_SENDER') then
      return nil, {['X-Rspamd-First-Sender'] = 'yes'}, {}, {}
    end
    return nil, {}, {}, {}
  end
EOD;
}
```

Notes:

* the header is added by Rspamd, not by first-contact: it uses a different
  name to avoid confusion;
* it applies to the whole message, not to each envelope recipient;
* do not move first-contact before Rspamd just to make the
  `X-First-Contact` header visible: spam rejected later would still be written
  to the database;
* the visible banner remains first-contact's job.

### 3.5 Source installation (/opt layout)

If you do not use the packages, the install script places the code in
`/opt/first-contact` and creates a wrapper with `PYTHONPATH`:

```bash
cd first-contact
./scripts/install.sh                          # show the plan, change nothing
./scripts/install.sh --apply                  # user, directories, service
./scripts/install.sh --apply --with-postfix   # + main.cf with backup
```

What `--apply` does:

| step | effect |
|---|---|
| 1 | `apt-get install python3-milter` if missing |
| 2 | system user `firstcontact` |
| 3 | `/var/lib/first-contact` (0700) and `/var/spool/postfix/first-contact` (2750 `firstcontact:postfix`) |
| 4 | code in `/opt/first-contact` |
| 5 | `/etc/first-contact/config.ini` and `whitelist.txt` (not overwritten if they exist) |
| 6 | `/usr/local/bin/first-contact` command |
| 7 | systemd unit + `PYTHONPATH` drop-in + tmpfiles + start |
| 8 | (`--with-postfix` only) append to `smtpd_milters`, back up `main.cf`, `postfix check`, reload |

Equivalent manual installation:

```bash
apt-get install -y python3-milter
useradd --system --home-dir /var/lib/first-contact --no-create-home \
        --shell /usr/sbin/nologin firstcontact
install -d -m 0700 -o firstcontact -g firstcontact /var/lib/first-contact
install -d -m 2750 -o firstcontact -g postfix /var/spool/postfix/first-contact
install -d -m 0755 /opt/first-contact
cp -a src/firstcontact /opt/first-contact/
install -m 0644 README.md /opt/first-contact/README.md
install -d -m 0755 /etc/first-contact
install -m 0640 -o root -g firstcontact etc/first-contact.ini /etc/first-contact/config.ini
install -m 0640 -o root -g firstcontact etc/whitelist.txt /etc/first-contact/whitelist.txt
install -m 0644 systemd/first-contact-milter.service /etc/systemd/system/
install -d -m 0755 /etc/systemd/system/first-contact-milter.service.d
install -m 0644 packaging/systemd-prefix.conf \
        /etc/systemd/system/first-contact-milter.service.d/prefix.conf
install -m 0644 etc/tmpfiles.d/first-contact.conf /etc/tmpfiles.d/
systemd-tmpfiles --create /etc/tmpfiles.d/first-contact.conf
systemctl daemon-reload && systemctl enable --now first-contact-milter
# then main.cf as in postfix/main.cf.snippet
```

## 4. Configuration

`/etc/first-contact/config.ini`, INI format (Python 3.7 has no TOML). After a
change: `systemctl restart first-contact-milter`. The **whitelist** reloads
by itself instead (see section 6).

| key | default | meaning |
|---|---|---|
| `[milter] socket` | `unix:/var/spool/postfix/first-contact/milter.sock` | socket; alternative `inet:11334@127.0.0.1` |
| `[milter] header_name` | `X-First-Contact` | name of the added header |
| `[milter] header_position` | `top` | `top` (automatic fallback if `insheader` is unavailable) or `end` |
| `[banner] mode` | `body` | `off` (header only), `body` (banner), `subject` (subject prefix), `both` |
| `[banner] text` | default warning | banner text, with `{sender}`, `{recipient}`, `{recipients}`, `{first_seen}`, `{client}` placeholders |
| `[banner] text_resumed` | "not heard from for N days" warning | text for the absent sender; additionally `{days}` and `{last_seen}` |
| `[banner] subject_tag` | empty | subject prefix (used with `subject`/`both`) |
| `[banner] max_body_bytes` | `20971520` | above this size the body is not rewritten (0 = no limit) |
| `[banner] skip_signed` | `true` | do not touch `multipart/signed` and `multipart/encrypted` |
| `[banner] html_box` | `true` | yellow/black HTML box with dashed border; `false` = text only |
| `[whitelist] senders` | empty | excluded addresses/domains, comma-separated |
| `[whitelist] networks` | empty | excluded client networks or IPs, comma-separated |
| `[whitelist] file` | empty | file with one entry per line, re-read when it changes |
| `[policy] key_source` | `envelope` | `envelope` = `MAIL FROM`, `from` = `From:` header |
| `[policy] key_scope` | `address` | `address` = per address, `domain` = per domain |
| `[policy] process_authenticated` | `false` | also evaluate mail sent with SASL |
| `[policy] process_local` | `false` | also evaluate mail without an SMTP client (cron, pickup) and from trusted networks |
| `[policy] trusted_networks` | `127.0.0.0/8, ::1` | networks considered internal |
| `[policy] skip_local_senders` | empty (example: local domains) | sender domains to ignore |
| `[policy] skip_null_sender` | `true` | ignore bounces (empty `MAIL FROM`) |
| `[policy] max_recipients` | `50` | recipient cap per message |
| `[policy] dormant_days` | `90` | absence in days after which the "resumed" warning triggers (0 = disabled) |
| `[policy] observe_only` | `false` | record but add no warning (warm-up) |
| `[store] path` | `/var/lib/first-contact/contacts.db` | SQLite database |
| `[store] track_stats` | `true` | update `last_seen`/`messages` (with `dormant_days > 0`, `last_seen` is updated anyway) |
| `[store] retention_days` | `0` | automatic cleanup of old contacts (0 = never) |
| `[logging] level` | `INFO` | `DEBUG` also shows "already known" |
| `[logging] syslog` | `true` | write to `/var/log/mail.log` (facility `mail`) |

### Recommended warm-up: `observe_only = true` for a week

The database starts empty: on the first day **every** external sender is
"new", so almost all incoming mail would carry the warning. The milter has an
observation mode that records contacts without adding anything:

```ini
[policy]
observe_only = true
```

After a few days of normal mail, set it to `false` and restart: regular
correspondents are already known and the warning fires only for genuinely new
senders.

## 5. The visible warning (banner)

With `[banner] mode = body` (default) the user sees a **yellow box with black
text and a black dashed border** at the top of the message text:

```text
+--------------------------------------------------------------+
|  ATTENZIONE: e' il primo messaggio che ricevi da ...          |  yellow background
|  Non e' detto che sia pericoloso, ma presta attenzione ad     |  black text
|  allegati e link: ...                                         |  dashed border
+--------------------------------------------------------------+
[ original content, attachments unchanged ]
```

The warning goes into the text parts the message already has: the `text/html`
part gets the inline-styled box (the only way to style mail clients), the
`text/plain` part gets the same warning in an ASCII dashed frame. In a
`multipart/alternative` both are updated, so the client always shows the
version it uses. The email structure does not change: no new `multipart`, no
attachment. To disable HTML and use text only: `html_box = false`.

The text is customisable in `[banner] text`; placeholders are replaced and
lines wrapped at 78 columns. A more explicit example:

```ini
[banner]
mode = both
subject_tag = [FIRST CONTACT]
text = Warning: first message from {sender} to {recipient} ({first_seen}).
       Check attachments and links carefully before opening them.
```

Safety choices:

* `multipart/signed` and `multipart/encrypted` are **not** rewritten
  (`skip_signed = true`): these messages get the header only, with no banner
  **and no subject prefix**, because either would visibly invalidate the
  signature or encryption.
* bodies above `max_body_bytes` and messages whose body was not received:
  fall back to the header only, never replacing the body with an empty one.
* any error during rewriting is logged and swallowed.

Parts with non-ASCII-compatible charsets (for example UTF-16/UTF-32) or
unknown charsets are left byte-for-byte unchanged. If no other text part can
be rewritten, the message receives the technical header only.

### 5.1 Sender absent for a long time (`dormant_days`)

Besides first contact, the milter flags the opposite case: a sender we
**know** who has not written for at least `[policy] dormant_days` days
(default **90**, `0` disables the rule). This helps spot messages that imitate
a regular correspondent after months of silence.

The default text is deliberately short and direct:

```text
ATTENZIONE: mario.rossi@example.com non ti scriveva da 100 giorni.
Verifica l'identita' del mittente prima di aprire allegati o link.
```

The box stays the same (yellow, dashed border); only the text changes.
Customisable in `[banner] text_resumed`, with the placeholders `{sender}`,
`{recipient}`, `{recipients}`, `{first_seen}`, `{client}` plus `{days}` (days
of absence) and `{last_seen}` (date of the last message).

```ini
[policy]
dormant_days = 90

[banner]
text_resumed = ATTENZIONE: {sender} non ti scriveva da {days} giorni.
               Verifica l'identita' del mittente prima di aprire allegati o link.
```

How it works:

* the threshold is evaluated **atomically** with message recording (`UPDATE
  ... WHERE last_seen <= threshold`): two identical messages arriving together
  produce **one** "resumed" warning;
* when `dormant_days > 0` the milter updates `last_seen` **always**, even with
  `track_stats = false`, otherwise a regular correspondent would look absent
  after the threshold;
* the header distinguishes the two cases: `kind=first-message` or
  `kind=resumed`, plus `last-seen=` and `days=`;
* with `observe_only = true` the case is logged but produces neither header
  nor banner;
* the whitelist takes precedence here too: an excluded sender raises no
  warning.

If you use `retention_days`, keep it greater than `dormant_days`: otherwise
pruning deletes the history before the rule can fire (`doctor` reports this).

### DKIM: a trade-off to know

Rewriting the body **invalidates the sender's DKIM signatures** for that
message: any change alters the body hash, and there is no way to add a banner
while keeping them valid. Practical consequences:

* DKIM checks performed **after** our milter (e.g. a later anti-spam filter)
  will see the sender's signature as invalid;
* if `opendkim` on the server also **signs** mail (`Mode s` or `sv` in
  `/etc/opendkim.conf`), it must be placed **after** first-contact in
  `smtpd_milters`, so the local signature covers the banner too:
  ```
  smtpd_milters = inet:127.0.0.1:11332 unix:/first-contact/milter.sock unix:/opendkim/opendkim.sock
  ```
  With `Mode v` (verification only, OpenDKIM's default) the order has no
  effect.
* with `[banner] mode = off` the body is untouched and signatures stay valid:
  this is the right choice if in-transit DKIM matters more than the visible
  warning.

## 6. Whitelist: excluded senders and networks

For some origins the warning is useless (or annoying). The whitelist excludes
them: they get **neither header nor banner** and nothing is written to the
database.

Accepted formats, both in `senders`/`networks` and in the file:

| entry | meaning |
|---|---|
| `mario.rossi@example.com` | exact address |
| `@example.com` | domain and all subdomains |
| `*.example.com` | as above |
| `example.com` | as above |
| `203.0.113.9` | single SMTP client IP |
| `10.0.0.0/8` | SMTP client CIDR network |
| `# comment` | file only |

```ini
[whitelist]
senders = postmaster@example.com, @example.org
networks = 10.0.0.0/8, 192.168.0.0/24
file = /etc/first-contact/whitelist.txt
```

One entry per line in the file:

```
# suppliers and home domains
@example.org
mario.rossi@example.com
10.0.0.0/8
```

Intentional differences between the two sources:

* entries in the **configuration** are validated at startup: an error stops
  the service (better to notice immediately);
* entries in the **file** are permissive: invalid lines are ignored and
  logged, without stopping anything;
* the file is **re-read automatically when it changes** (mtime/size): adding
  or removing entries needs no restart.

Logs state the exclusion reason explicitly: `whitelist-address`,
`whitelist-domain`, `whitelist-cidr`. The whitelist wins over everything: a
whitelisted sender raises no warning even if authenticated or internal.

## 7. Verification

```bash
first-contact doctor          # config, DB, socket, main.cf, milter order, banner, whitelist
systemctl status first-contact-milter --no-pager
journalctl -u first-contact-milter -n 30 --no-pager
```

Run diagnostic commands as **root** (or as the `firstcontact` user): with the
`/opt` installation the configuration is readable only by them; with the
packages it is world-readable, but the database and whitelist remain
protected by their directories.

Decision test without sending mail:

```bash
# state of a pair in the real database (read-only)
first-contact check --sender mario.rossi@example.com \
    --recipient anna@example.it --client 203.0.113.9
# -> NEW ... or KNOWN ... with absence=Nd and the dormant_days threshold;
#    if whitelisted: EXCLUDED ...

# simulation on an .eml: shows header, banner and the start of the rewritten message
# (temporary database, the real one is untouched)
first-contact simulate --eml examples/sample.eml \
    --recipient anna@example.it --client 203.0.113.9

# test the "absent sender" rule: pretends a contact happened 100 days ago
first-contact simulate --eml examples/sample.eml \
    --recipient anna@example.it --client 203.0.113.9 --known-days 100
# -> alert type: sender returned after 100 days
```

Real test delivery:

```bash
printf 'Subject: first-contact test\n\nhello\n' | \
  swaks --to anna@example.it --from mario.rossi@example.com \
        --server 127.0.0.1 --header 'From: Mario Rossi <mario.rossi@example.com>'
```

The first message should have the banner and header, the second should not. In
`/var/log/mail.log`:

```
first-contact[...]: first-contact sender=mario.rossi@example.com recipient=anna@example.it client=203.0.113.9
```

### Client-side rule (optional)

The header remains useful for filtering, even with the banner enabled:

For messages with multiple envelope recipients, all alert headers use
`recipient=undisclosed`, and `{recipient}` / `{recipients}` in banner templates
are empty. This also applies when the recipient processing cap is reached:
Ccn/Bcc addresses must never be added to a shared message. Database tracking
remains per sender/recipient pair. Address-based client rules below apply
only to messages with one envelope recipient; for shared messages match
`X-First-Contact` itself, which indicates an alert for at least one recipient.

* **Outlook**: Rules -> "with specific words in the message header" ->
  `X-First-Contact` -> move to "First contact".
* **Thunderbird**: Filters -> "Customize" -> `X-First-Contact` -> "contains"
  -> move/colour.
* **Dovecot/Sieve** (if Sieve is added later):
  ```
  if header :contains "X-First-Contact" "recipient=anna@example.it" {
      fileinto "First contact";
      addflag "\\Flagged";
  }
  ```
* **Procmail**: `* ^X-First-Contact: .*recipient=anna@example\.it`

The header contains `kind=first-message` or `kind=resumed`: to tell the two
cases apart, look for `resumed` in the value (and `days=` for the days of
absence).

## 8. Scope: what is and is not evaluated

* Only mail arriving over SMTP from an external client.
* Skipped: SASL-authenticated sessions (`submission`, internal users), clients
  in `trusted_networks` (including loopback), local mail without a client
  (`cron`, `pickup`), null sender (bounces), senders in `skip_local_senders`
  domains, and of course whitelisted senders/networks.
* To evaluate internal mail as well: `process_authenticated = true`,
  `process_local = true` and `trusted_networks =` (empty).

## 9. Known limitations

1. **Multi-recipient messages**: there is one message in the queue and it is
   delivered identically to everyone; the banner is therefore the same for
   all. Headers use `recipient=undisclosed` and banner recipient placeholders
   are empty, protecting Bcc addresses. An alert applies to at least one
   recipient, including when another recipient already knows the sender. Strictly per-mailbox
   semantics would require acting at delivery time (per-user LDA/LMTP), not in
   a milter.
2. **Aliases and groups**: the key is the *envelope* recipient; if an alias
   expands to several mailboxes, the warning is computed on the alias, not on
   the final mailboxes.
3. **Forgeable envelope**: `MAIL FROM` is declared by the sender. With
   `key_source = envelope` it is hard to alter in the content, but a sender
   can still use different addresses each time to avoid looking "new". The
   database is not an anti-spam system, it is a reminder.
4. **DKIM**: with the banner enabled the sender's signatures are invalid
   (section 5). With `mode = off` the problem does not exist.
5. **Signed/encrypted messages**: not rewritten (neither body nor subject);
   they get the header only.
6. **Empty database on first start**: use `observe_only` for the warm-up
   (section 4).
7. **Retention**: `retention_days` is not a scheduler; run
   `first-contact prune` from cron/systemd timer if desired.
8. **IMAP clients**: the banner is inserted into the message text and shown as
   a normal body; the `X-First-Contact` header does not appear in the preview
   but is available to rules.

## 10. Maintenance

```bash
first-contact stats                 # pairs, senders, recipients, messages
first-contact list --limit 20       # latest contacts, with dates
first-contact prune --days 730 --dry-run
first-contact prune --days 730
```

Read commands (`doctor`, `check`, `list`, `stats`, and `prune --dry-run`)
open an existing database without initializing it. Before the first message,
a missing database is reported as unavailable and is left for the service to
create. When run as root with the service account installed, administrative
queries and pruning use the `firstcontact` identity, so SQLite WAL sidecars
also retain the correct owner. A custom configuration must be readable by
that account.



Backup (SQLite in WAL mode, online):

```bash
sqlite3 /var/lib/first-contact/contacts.db ".backup /root/contacts-$(date +%F).db"
```

To wipe everything and start over: stop the service, remove `contacts.db`,
`contacts.db-wal`, `contacts.db-shm`, restart.

## 11. Uninstallation

With the packages:

```bash
sudo dpkg -r first-contact      # Debian/Ubuntu
sudo dnf remove first-contact   # Enterprise Linux
```

`/etc/first-contact` and `/var/lib/first-contact` remain (configuration and
database); remove them by hand if no longer needed. Remove the token from
`smtpd_milters` before or after, with a Postfix reload.

With the source installation:

```bash
./scripts/uninstall.sh --apply            # service, code, config; DB kept
./scripts/uninstall.sh --apply --purge    # also removes the database
```

The token is removed from `smtpd_milters` with a `main.cf` backup and a
Postfix reload.

## 12. Tests

```bash
./scripts/run-tests.sh
```

They cover: address normalisation, header injection, scope rules, first
time / later time / sender returning after an absence, the `dormant_days`
threshold (inclusive and disabled), concurrency on the INSERT and on the
"resumed" decision, pruning, whitelist parsing and reload, banner text and
body MIME rewriting (HTML box, text fallback, base64 and multipart),
`{days}`/`{last_seen}` placeholders, a full SMTP transaction simulation with a
fake `Milter` module, subject prefix, skipped signed messages, fallback on an
oversized body, fallback when `insheader` is unavailable, and the fact that a
decider failure never interrupts mail.

## 13. License

GPL-2.0-or-later. The full text is in `LICENSE`.
