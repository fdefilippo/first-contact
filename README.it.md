# first-contact

English: [README.md](README.md).

Milter per Postfix che avvisa un destinatario quando riceve **per la prima
volta** da un certo mittente, oppure quando un mittente **noto ricompare dopo
un'assenza prolungata** (90 giorni per default, configurabile).

L'avviso e' **visibile all'utente**, non solo tecnico:

* un **banner in cima al corpo** del messaggio (predefinito), che invita a
  prestare attenzione ad allegati e link;
* in alternativa o in aggiunta, un **prefisso nell'oggetto**;
* sempre, un header `X-First-Contact` utilizzabile nelle regole del client.

```
ATTENZIONE: e' il primo messaggio che ricevi da mario.rossi@example.com.
Non e' detto che sia pericoloso, ma presta attenzione ad allegati e link:
non aprire file e non cliccare collegamenti se non ti fidi del mittente.
```

Se il milter e' guasto o spento la posta continua a passare
(`milter_default_action = accept`). Con l'avviso nel corpo il contenuto
originale resta al suo posto: il banner viene inserito nelle parti testuali
gia' presenti, senza creare nuove parti MIME e senza trasformare il messaggio
in un allegato. HTML, allegati e codifiche restano intatti.

## 1. Come funziona

```
                 +-----------+   milter    +-------------------+
mittente --> smtpd --> rspamd --> opendkim --> first-contact --> coda --> LMTP/LDA
                                                      |
                                                      v
                                        /var/lib/first-contact/contacts.db
                                        tabella contacts(sender, recipient, ...)
```

* Il milter riceve da Postfix mittente **envelope** (`MAIL FROM`), destinatari
  (`RCPT TO`), header e corpo del messaggio, alla fine dei dati (`eom`).
* Per ogni coppia (mittente, destinatario) esegue un `INSERT OR IGNORE` su
  SQLite: se la riga viene inserita e' la prima volta. L'operazione e'
  atomica, quindi due copie dello stesso messaggio arrivate insieme non
  producono due allerte. In alternativa la stessa query distingue il mittente
  **assente da troppo tempo** (vedi paragrafo 5.1): anche in quel caso solo il
  primo dei messaggi simultanei produce l'avviso.
* La whitelist viene consultata **prima** di tutto: se mittente o rete del
  client corrispondono, il messaggio non riceve alcun avviso e non viene
  scritto nel database.
* Se la coppia e' nuova aggiunge un header per **ogni destinatario nuovo**
  (un messaggio con tre destinatari nuovi riceve tre header, uno per
  destinatario) e, se previsto, l'avviso visibile (banner e/o oggetto).
* Ogni errore (database bloccato, eccezione, socket, corpo troppo grande)
  viene registrato e assorbito: il milter risponde sempre `CONTINUE`, la posta
  non si ferma. Se l'avviso visibile non e' applicabile resta l'header.

Perche' un milter e non `header_checks`: `header_checks` non conosce lo stato
per coppia mittente/destinatario e non puo' consultare un database. Un
`content_filter` comporterebbe una seconda iniezione del messaggio e la
perdita delle informazioni di envelope. Per modificare il corpo in modo
selettivo serve un milter.

### Come viene aggiunto il banner

L'avviso **non crea nuove parti MIME** e non cambia il `Content-Type` del
messaggio: viene inserito in cima alle parti testuali gia' presenti.

```
text/plain                  <- avviso, poi il testo originale
text/html                   <- box giallo subito dopo <body>, poi l'HTML originale
multipart/alternative       <- l'avviso entra in entrambe le versioni
multipart/mixed             <- il testo riceve l'avviso, gli allegati restano allegati
```

Per ogni parte toccata si decodifica solo il transfer encoding (`base64`,
`quoted-printable`, `7bit`/`8bit`), si aggiunge il testo e si riapplica la
stessa codifica. Header, boundary e struttura restano quelli originali: il
client mostra un normale messaggio leggibile, senza allegati aggiunti e senza
un `.eml` incorporato. Gli allegati (`Content-Disposition: attachment`) e i
messaggi inoltrati (`message/rfc822`) non vengono toccati. E' lo stesso
principio usato dai tool per i disclaimer, per esempio alterMIME.

`multipart/signed` e `multipart/encrypted` non vengono riscritti: qualunque
modifica invaliderebbe firma o cifratura.

## 2. Contenuto del progetto

```
src/firstcontact/       pacchetto Python (solo stdlib + pymilter per "serve")
  config.py             lettura/validazione della configurazione INI
  policy.py             normalizzazione indirizzi, regole "cosa guardare"
  headers.py            costruzione header sicura (ASCII, folding RFC 5322)
  banner.py             testo dell'avviso e riscrittura MIME del corpo
  whitelist.py          mittenti/reti esclusi, con ricarica automatica
  store.py              SQLite, atomicita' del "primo contatto"
  engine.py             decisione: header, banner, prefisso oggetto
  milter_app.py         collante con pymilter (callback SMTP)
  cli.py                serve / check / list / stats / prune / doctor / simulate
etc/first-contact.ini   configurazione di esempio
etc/whitelist.txt       whitelist di esempio (una voce per riga)
etc/tmpfiles.d/         permessi di directory al boot
systemd/                unita' di servizio
postfix/                modifica a main.cf (note su chroot, ordine e DKIM)
packaging/              launcher del comando, drop-in systemd, spec RPM
debian/                 packaging Debian (debhelper, compat 12)
man/                    pagina di manuale first-contact(8)
scripts/install.sh      installazione da sorgente in /opt (dry-run di default)
scripts/uninstall.sh    rimozione
scripts/run-tests.sh    test
scripts/build-deb.sh    pacchetto .deb
scripts/build-rpm.sh    pacchetto .rpm noarch
.github/workflows/      build dei pacchetti e release su tag
tests/                  161 test (unittest, nessuna dipendenza)
examples/sample.eml     messaggio di prova
```

## 3. Installazione

Requisiti: Postfix, Python >= 3.6 e `pymilter` (`python3-milter` su Debian,
`python3-pymilter` su Enterprise Linux, da EPEL). I pacchetti installano il
comando, la configurazione, l'unita' systemd e le regole `tmpfiles.d`; creano
l'utente di servizio `firstcontact` e **abilitano e avviano**
`first-contact-milter`. Non modificano Postfix: il milter va aggiunto a
`smtpd_milters` (paragrafo 3.3).

### 3.1 Pacchetti deb e rpm

Debian e Ubuntu (architettura `all`):

```bash
sudo apt-get install -y python3-milter
sudo dpkg -i first-contact_1.0.2_all.deb
```

Il `.deb` e' compresso con `xz` e non usa `zstd`: si installa anche su
Debian 10. Su buster (EOL) `python3-milter` va preso da `archive.debian.org`
(vedi la nota in `scripts/install.sh`).

Enterprise Linux 8, 9 e 10 (noarch; `pymilter` arriva da EPEL):

```bash
sudo dnf install -y epel-release
sudo dnf install ./first-contact-1.0.2-1.el8.noarch.rpm
```

Dove finisce il codice:

| pacchetto | codice Python | comando | configurazione |
|---|---|---|---|
| deb | `/usr/lib/python3/dist-packages/firstcontact` | `/usr/bin/first-contact` | `/etc/first-contact/config.ini` |
| rpm | `/usr/lib/python3.X/site-packages/firstcontact` | `/usr/bin/first-contact` | `/etc/first-contact/config.ini` |

I file di configurazione sono installati come `conffile` (`%config(noreplace)`
negli RPM): gli aggiornamenti non li sovrascrivono. Non contengono segreti,
quindi sono leggibili da tutti; la whitelist e il database restano protetti
dai permessi delle loro directory.

Verifica dopo l'installazione:

```bash
first-contact doctor
systemctl status first-contact-milter --no-pager
```

### 3.2 Costruire i pacchetti

Servono `debhelper` (>= 12) e `dh-python` per il deb, `rpm-build` e
`python3-rpm-macros` per l'RPM. Gli script usano la versione dichiarata in
`src/firstcontact/__init__.py` e scrivono in `dist/`.

```bash
./scripts/run-tests.sh
./scripts/build-deb.sh     # dist/first-contact_1.0.2_all.deb
./scripts/build-rpm.sh     # dist/first-contact-1.0.2-1.el*.noarch.rpm
```

L'RPM va costruito dentro la versione target di Enterprise Linux (i bytecode
e i tag `%{?dist}` dipendono dalla release):

```bash
podman run --rm -v "$PWD:/src:z" -w /src rockylinux:8 \
  bash -c 'dnf install -y rpm-build python3-rpm-macros && ./scripts/build-rpm.sh'
```

Il workflow `.github/workflows/build-packages.yml` fa lo stesso in CI: artifact
a ogni push e pull request, pacchetti allegati alla release GitHub per i tag
`v*` (creata con `gh release create`, senza action di terze parti).

### 3.3 Postfix: aggiungere il milter

Il token da aggiungere **in coda** a `smtpd_milters` e':

```
unix:/first-contact/milter.sock
```

Il socket unix e' **dentro** `/var/spool/postfix` perche' `smtpd` gira in
chroot (`master.cf`, colonna chroot = `y`): da dentro il chroot il percorso e'
`/first-contact/milter.sock`, sul filesystem
`/var/spool/postfix/first-contact/milter.sock`. Se si sposta il socket fuori da
`/var/spool/postfix`, Postfix non lo vede piu'.

```bash
cp -a /etc/postfix/main.cf /etc/postfix/main.cf.first-contact.$(date +%F)
postconf -e "smtpd_milters = $(postconf -h smtpd_milters) unix:/first-contact/milter.sock"
postfix check && systemctl reload postfix
```

Il milter va dopo `rspamd` (i messaggi rifiutati non vanno registrati) e, se
`opendkim` **firma** la posta (`Mode s`/`sv`), prima di `opendkim` (la firma
locale copre anche il banner). Dettagli e verifica in
`postfix/main.cf.snippet`.

### 3.4 Integrazione con Rspamd (facoltativa)

first-contact lavora gia' accanto a Rspamd: sono entrambi milter Postfix e basta
l'ordine in `smtpd_milters`. Tenere first-contact dopo Rspamd, cosi' i messaggi
rifiutati come spam non vengono registrati:

```
smtpd_milters = inet:127.0.0.1:11332 unix:/first-contact/milter.sock
```

Il modulo `known_senders` di Rspamd e' la funzione nativa piu' vicina: mantiene
in Redis un insieme di mittenti per i domini mittente configurati (di solito i
domini di posta gratuita) e imposta `KNOWN_SENDER` / `UNKNOWN_SENDER`. Non e'
equivalente a first-contact: e' globale, non per coppia mittente/destinatario,
non ha la regola dei giorni di assenza e non aggiunge il banner.

Se serve solo un header nativo Rspamd per il primo mittente, `milter_headers`
puo' esporre quel simbolo. E' indipendente da first-contact: il milter resta
necessario per la precisione per destinatario e per il banner nel corpo.

`/etc/rspamd/local.d/known_senders.conf`:

```
enabled = true;
domains = "/etc/rspamd/local.d/known_senders_domains.map";
#servers = "127.0.0.1:6379";
```

La mappa contiene un dominio mittente per riga. Redis deve essere configurato
per Rspamd.

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

Note:

* l'header lo aggiunge Rspamd, non first-contact: usa un nome diverso per
  evitare confusione;
* vale per l'intero messaggio, non per ogni destinatario envelope;
* non spostare first-contact prima di Rspamd solo per rendere visibile
  l'header `X-First-Contact`: la posta rifiutata dopo verrebbe comunque scritta
  nel database;
* il banner visibile resta compito di first-contact.

### 3.5 Installazione da sorgente (layout in /opt)

Chi non usa i pacchetti puo' installare lo script, che mette il codice in
`/opt/first-contact` e crea un wrapper con `PYTHONPATH`:

```bash
cd first-contact
./scripts/install.sh                          # mostra il piano, non tocca nulla
./scripts/install.sh --apply                  # utente, directory, servizio
./scripts/install.sh --apply --with-postfix   # + main.cf con backup
```

Cosa fa `--apply`:

| passo | effetto |
|---|---|
| 1 | `apt-get install python3-milter` se manca |
| 2 | utente di sistema `firstcontact` |
| 3 | `/var/lib/first-contact` (0700) e `/var/spool/postfix/first-contact` (2750 `firstcontact:postfix`) |
| 4 | codice in `/opt/first-contact` |
| 5 | `/etc/first-contact/config.ini` e `whitelist.txt` (non sovrascritti se esistono) |
| 6 | comando `/usr/local/bin/first-contact` |
| 7 | unit systemd + drop-in `PYTHONPATH` + tmpfiles + avvio |
| 8 | (solo `--with-postfix`) aggiunta in coda a `smtpd_milters`, backup di `main.cf`, `postfix check`, reload |

Installazione manuale equivalente:

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
# poi main.cf come in postfix/main.cf.snippet
```

## 4. Configurazione

`/etc/first-contact/config.ini`, formato INI (Python 3.7 non ha TOML).
Dopo una modifica: `systemctl restart first-contact-milter`. La **whitelist**
invece si ricarica da sola (vedi paragrafo 6).

| chiave | default | significato |
|---|---|---|
| `[milter] socket` | `unix:/var/spool/postfix/first-contact/milter.sock` | socket; alternativa `inet:11334@127.0.0.1` |
| `[milter] header_name` | `X-First-Contact` | nome header aggiunto |
| `[milter] header_position` | `top` | `top` (con fallback automatico se `insheader` non c'e') o `end` |
| `[banner] mode` | `body` | `off` (solo header), `body` (banner), `subject` (prefisso oggetto), `both` |
| `[banner] text` | avviso predefinito | testo del banner, con segnaposto `{sender}`, `{recipient}`, `{recipients}`, `{first_seen}`, `{client}` |
| `[banner] text_resumed` | avviso "non ti scriveva da N giorni" | testo per il mittente assente; in piu' `{days}` e `{last_seen}` |
| `[banner] subject_tag` | vuoto | prefisso per l'oggetto (usato con `subject`/`both`) |
| `[banner] max_body_bytes` | `20971520` | oltre questa dimensione il corpo non viene riscritto (0 = nessun limite) |
| `[banner] skip_signed` | `true` | non toccare `multipart/signed` e `multipart/encrypted` |
| `[banner] html_box` | `true` | box HTML giallo/nero con bordo tratteggiato; `false` = solo testo |
| `[whitelist] senders` | vuoto | indirizzi/domini esclusi, separati da virgola |
| `[whitelist] networks` | vuoto | reti o IP del client esclusi, separati da virgola |
| `[whitelist] file` | vuoto | file con una voce per riga, riletto quando cambia |
| `[policy] key_source` | `envelope` | `envelope` = `MAIL FROM`, `from` = header `From:` |
| `[policy] key_scope` | `address` | `address` = per indirizzo, `domain` = per dominio |
| `[policy] process_authenticated` | `false` | valutare anche la posta inviata con SASL |
| `[policy] process_local` | `false` | valutare anche la posta senza client SMTP (cron, pickup) e dalle reti fidate |
| `[policy] trusted_networks` | `127.0.0.0/8, ::1` | reti considerate interne |
| `[policy] skip_local_senders` | vuoto (nell'esempio: i domini locali) | domini di mittente ignorati |
| `[policy] skip_null_sender` | `true` | ignora i rimbalzi (`MAIL FROM` vuoto) |
| `[policy] max_recipients` | `50` | tetto destinatari per messaggio |
| `[policy] dormant_days` | `90` | giorni di assenza oltre i quali scatta l'avviso "resumed" (0 = disattivata) |
| `[policy] observe_only` | `false` | registra ma non aggiunge alcun avviso (rodaggio) |
| `[store] path` | `/var/lib/first-contact/contacts.db` | database SQLite |
| `[store] track_stats` | `true` | aggiorna `last_seen`/`messages` (con `dormant_days > 0` `last_seen` viene aggiornato comunque) |
| `[store] retention_days` | `0` | pulizia automatica dei contatti vecchi (0 = mai) |
| `[logging] level` | `INFO` | `DEBUG` per vedere anche i "gia' noti" |
| `[logging] syslog` | `true` | scrive in `/var/log/mail.log` (facility `mail`) |

### Rodaggio consigliato: `observe_only = true` per una settimana

Il database parte vuoto: al primo giorno **ogni** mittente esterno e' "nuovo",
quindi quasi tutta la posta in arrivo avrebbe l'avviso. Il milter ha una
modalita' di osservazione che registra i contatti senza aggiungere nulla:

```ini
[policy]
observe_only = true
```

Dopo qualche giorno di posta normale si passa a `false` e si riavvia: i
corrispondenti abituali sono gia' noti e l'allerta scatta solo per i mittenti
davvero nuovi.

## 5. L'avviso visibile (banner)

Con `[banner] mode = body` (predefinito) l'utente vede in cima al testo del
messaggio un **box con sfondo giallo, testo nero e bordo tratteggiato nero**:

```text
+--------------------------------------------------------------+
|  ATTENZIONE: e' il primo messaggio che ricevi da ...          |   sfondo giallo
|  Non e' detto che sia pericoloso, ma presta attenzione ad     |   testo nero
|  allegati e link: ...                                         |   bordo tratteggiato
+--------------------------------------------------------------+
[ contenuto originale, con allegati, invariato ]
```

L'avviso entra nelle parti testuali che il messaggio ha gia': la parte
`text/html` riceve il box con stile inline (l'unico modo per avere uno stile
nei client di posta), la parte `text/plain` riceve la stessa allerta con una
cornice tratteggiata in ASCII. In un `multipart/alternative` vengono
aggiornate entrambe, quindi il client mostra sempre la versione che usa. La
struttura dell'email non cambia: nessun nuovo `multipart`, nessun allegato.
Per disattivare l'HTML e usare soltanto il testo: `html_box = false`.

Il testo e' personalizzabile in `[banner] text`; i segnaposto vengono
sostituiti e le righe impaginate a 78 colonne. Un esempio piu' esplicito:

```ini
[banner]
mode = both
subject_tag = [PRIMO CONTATTO]
text = Attenzione: primo messaggio da {sender} per {recipient} ({first_seen}).
       Controlla con cura allegati e link prima di aprirli.
```

Scelte di sicurezza:

* `multipart/signed` e `multipart/encrypted` **non** vengono riscritti
  (`skip_signed = true`): per questi messaggi resta il solo header, senza
  banner **e senza prefisso nell'oggetto**, perche' entrambi invaliderebbero la
  firma o la cifratura in modo evidente.
* corpi oltre `max_body_bytes` e messaggi di cui non e' stato ricevuto il
  corpo: si ripiega sul solo header, senza mai sostituire il corpo con vuoto.
* qualsiasi errore durante la riscrittura viene assorbito e registrato.

### 5.1 Mittente assente da tempo (`dormant_days`)

Oltre al primo contatto, il milter segnala il caso opposto: un mittente che
**conosciamo** ma che non si faceva vivo da almeno `[policy] dormant_days`
giorni (default **90**, `0` disattiva la regola). E' utile per accorgersi di
messaggi che imitano un corrispondente abituale dopo mesi di silenzio.

Il testo predefinito e' volutamente breve e diretto:

```text
ATTENZIONE: mario.rossi@example.com non ti scriveva da 100 giorni.
Verifica l'identita' del mittente prima di aprire allegati o link.
```

Il box resta lo stesso (giallo, bordo tratteggiato); cambia solo il testo.
Personalizzabile in `[banner] text_resumed`, con i segnaposto `{sender}`,
`{recipient}`, `{recipients}`, `{first_seen}`, `{client}` piu' `{days}`
(giorni di assenza) e `{last_seen}` (data dell'ultimo messaggio).

```ini
[policy]
dormant_days = 90

[banner]
text_resumed = ATTENZIONE: {sender} non ti scriveva da {days} giorni.
               Verifica l'identita' del mittente prima di aprire allegati o link.
```

Come funziona:

* la soglia e' valutata in modo **atomico** insieme alla registrazione del
  messaggio (`UPDATE ... WHERE last_seen <= soglia`): due messaggi identici
  arrivati insieme producono **un solo** avviso "resumed";
* quando `dormant_days > 0` il milter aggiorna `last_seen` **sempre**, anche
  con `track_stats = false`, altrimenti un corrispondente abituale
  sembrerebbe assente dopo la soglia;
* l'header distingue i due casi: `kind=first-message` oppure `kind=resumed`,
  con in piu' `last-seen=` e `days=`;
* con `observe_only = true` il caso viene registrato nei log ma non produce
  ne' header ne' banner;
* la whitelist ha la precedenza anche qui: un mittente escluso non genera
  nessun avviso.

Se si usa `retention_days`, tenerlo maggiore di `dormant_days`: altrimenti il
pruning cancella lo storico prima che la regola possa scattare (`doctor`
segnala il caso).

Scelte di sicurezza:

### DKIM: compromesso da conoscere

Riscrivere il corpo **invalida le firme DKIM del mittente** per quel
messaggio: qualunque modifica cambia l'hash del corpo, non c'e' modo di
aggiungere un banner mantenendole valide. Conseguenze pratiche:

* i controlli DKIM fatti **dopo** il nostro milter (es. un filtro antispam
  successivo) vedranno la firma del mittente come non valida;
* se anche `opendkim` sul server **firma** la posta (`Mode s` o `sv` in
  `/etc/opendkim.conf`), va messo **dopo** first-contact in `smtpd_milters`,
  cosi' la firma locale copre anche il banner:
  ```
  smtpd_milters = inet:127.0.0.1:11332 unix:/first-contact/milter.sock unix:/opendkim/opendkim.sock
  ```
  Con `Mode v` (sola verifica, default di OpenDKIM) l'ordine non ha effetti.
* con `[banner] mode = off` il corpo non viene toccato e le firme restano
  valide: e' la scelta da fare se il DKIM in transito e' piu' importante
  dell'avviso visibile.

## 6. Whitelist: mittenti e reti esclusi

Per alcune origini l'avviso e' inutile (o fastidioso). La whitelist le
esclude: per loro non viene aggiunto **ne' header ne' banner** e non viene
scritto nulla nel database.

Formati accettati, sia in `senders`/`networks` sia nel file:

| voce | significato |
|---|---|
| `mario.rossi@example.com` | indirizzo esatto |
| `@example.com` | dominio e tutti i sottodomini |
| `*.example.com` | come sopra |
| `example.com` | come sopra |
| `203.0.113.9` | singolo IP del client SMTP |
| `10.0.0.0/8` | rete CIDR del client SMTP |
| `# commento` | solo nel file |

```ini
[whitelist]
senders = postmaster@example.com, @example.org
networks = 10.0.0.0/8, 192.168.0.0/24
file = /etc/first-contact/whitelist.txt
```

Nel file una voce per riga:

```
# fornitori e domini di casa
@example.org
mario.rossi@example.com
10.0.0.0/8
```

Differenze volute tra le due fonti:

* le voci nella **configurazione** sono validate all'avvio: un errore blocca
  il servizio (meglio accorgersene subito);
* le voci nel **file** sono permissive: le righe non valide vengono ignorate
  e segnalate nei log, senza fermare nulla;
* il file viene **riletto automaticamente quando cambia** (mtime/dimensione):
  aggiungere o togliere voci non richiede riavvii.

Nei log il motivo di esclusione e' esplicito: `whitelist-address`,
`whitelist-domain`, `whitelist-cidr`. La whitelist ha precedenza su tutto: un
mittente in whitelist non produce avvisi nemmeno se autenticato o interno.

## 7. Verifica

```bash
first-contact doctor          # config, DB, socket, main.cf, ordine milter, banner, whitelist
systemctl status first-contact-milter --no-pager
journalctl -u first-contact-milter -n 30 --no-pager
```

I comandi diagnostici vanno eseguiti come **root** (o come utente
`firstcontact`): con l'installazione in `/opt` la configurazione e' leggibile
solo da loro, con i pacchetti e' leggibile da tutti ma il database e la
whitelist restano protetti dalle directory.

Test di decisione senza inviare posta:

```bash
# stato di una coppia nel database reale (sola lettura)
first-contact check --sender mario.rossi@example.com \
    --recipient anna@example.it --client 203.0.113.9
# -> NEW ... oppure KNOWN ... con assenza=Ngg e la soglia dormant_days;
#    se in whitelist: ESCLUSO ...

# simulazione su un .eml: mostra header, banner e inizio del messaggio riscritto
# (database temporaneo, non tocca quello reale)
first-contact simulate --eml examples/sample.eml \
    --recipient anna@example.it --client 203.0.113.9

# prova la regola "mittente assente": finge un contatto avvenuto 100 giorni fa
first-contact simulate --eml examples/sample.eml \
    --recipient anna@example.it --client 203.0.113.9 --known-days 100
# -> tipo allerta: mittente tornato dopo 100 giorni
```

Invio reale di prova:

```bash
printf 'Subject: prova first-contact\n\nciao\n' | \
  swaks --to anna@example.it --from mario.rossi@example.com \
        --server 127.0.0.1 --header 'From: Mario Rossi <mario.rossi@example.com>'
```

Il primo messaggio deve avere banner e header, il secondo no. In
`/var/log/mail.log`:

```
first-contact[...]: first-contact sender=mario.rossi@example.com recipient=anna@example.it client=203.0.113.9
```

### Regola lato client (facoltativa)

L'header resta utile per filtrare, anche con il banner attivo:

* **Outlook**: Regole -> "con parole specifiche nell'intestazione del
  messaggio" -> `X-First-Contact` -> sposta in "Primo contatto".
* **Thunderbird**: Filtri -> "Personalizza" -> `X-First-Contact` ->
  "contiene" -> sposta/cambia colore.
* **Dovecot/Sieve** (se un giorno si aggiunge Sieve):
  ```
  if header :contains "X-First-Contact" "recipient=anna@example.it" {
      fileinto "Primo contatto";
      addflag "\\Flagged";
  }
  ```
* **Procmail**: `* ^X-First-Contact: .*recipient=anna@example\.it`

L'header contiene `kind=first-message` oppure `kind=resumed`: per distinguere i
due casi basta cercare `resumed` nel valore (e `days=` per i giorni di
assenza).

## 8. Ambito: cosa viene e cosa non viene valutato

* Solo posta che arriva via SMTP da un client esterno.
* Saltati: sessioni autenticate SASL (`submission`, utenti interni), client in
  `trusted_networks` (loopback incluso), posta locale senza client (`cron`,
  `pickup`), mittente nullo (rimbalzi), mittenti nei domini
  `skip_local_senders`, e naturalmente mittenti/reti in whitelist.
* Se serve valutare anche la posta interna: `process_authenticated = true`,
  `process_local = true` e `trusted_networks =` (vuoto).

## 9. Limiti noti

1. **Messaggi a piu' destinatari**: il messaggio e' uno solo in coda e viene
   consegnato uguale a tutti; il banner e' quindi lo stesso per tutti e ogni
   casella vede gli header di *tutti* i destinatari nuovi. La regola lato
   client deve cercare il proprio indirizzo (`recipient=...`). Per una
   semantica strettamente per-casella servirebbe agire in fase di consegna
   (LDA/LMTP per utente), non in milter.
2. **Alias e gruppi**: la chiave e' il destinatario *envelope*; se un alias
   espande verso piu' caselle, l'allerta e' calcolata sull'alias, non sulle
   caselle finali.
3. **Envelope falsificabile**: `MAIL FROM` e' dichiarato dal mittente. Con
   `key_source = envelope` e' difficile da alterare nel contenuto, ma un
   mittente puo' comunque usare indirizzi sempre diversi per non risultare
   "nuovo". Il database non e' un sistema antispam, e' un promemoria.
4. **DKIM**: con il banner attivo le firme del mittente risultano non valide
   (paragrafo 5). Con `mode = off` il problema non esiste.
5. **Messaggi firmati/cifrati**: non vengono riscritti (ne' corpo ne'
   oggetto); ricevono solo l'header.
6. **Database vuoto al primo avvio**: usare `observe_only` per il rodaggio
   (paragrafo 4).
7. **`Retention`**: `retention_days` non e' una pianificazione automatica; va
   invocato `first-contact prune` da cron/systemd timer se lo si desidera.
8. **Client IMAP**: il banner e' inserito nel testo del messaggio e viene
   mostrato come corpo normale; l'header `X-First-Contact` invece non appare
   nell'anteprima ma e' disponibile nelle regole.

## 10. Manutenzione

```bash
first-contact stats                 # coppie, mittenti, destinatari, messaggi
first-contact list --limit 20       # ultimi contatti, con date
first-contact prune --days 730 --dry-run
first-contact prune --days 730
```

Backup (SQLite in modalita' WAL, a caldo):

```bash
sqlite3 /var/lib/first-contact/contacts.db ".backup /root/contacts-$(date +%F).db"
```

Per svuotare tutto e ripartire: fermare il servizio, rimuovere
`contacts.db`, `contacts.db-wal`, `contacts.db-shm`, riavviare.

## 11. Disinstallazione

Con i pacchetti:

```bash
sudo dpkg -r first-contact      # Debian/Ubuntu
sudo dnf remove first-contact   # Enterprise Linux
```

`/etc/first-contact` e `/var/lib/first-contact` restano (configurazione e
database); rimuoverli a mano se non servono piu'. Il token va tolto da
`smtpd_milters` prima o dopo, con reload di Postfix.

Con l'installazione da sorgente:

```bash
./scripts/uninstall.sh --apply            # servizio, codice, config; DB conservato
./scripts/uninstall.sh --apply --purge    # elimina anche il database
```

Il token viene tolto da `smtpd_milters` con backup di `main.cf` e reload di
Postfix.

## 12. Test

```bash
./scripts/run-tests.sh
```

Coprono: normalizzazione indirizzi, injection negli header, regole di ambito,
prima volta / volta successiva / mittente tornato dopo un'assenza, soglia
`dormant_days` (inclusa e disattivata), concorrenza sull'INSERT e sulla
decisione "resumed", pruning, parsing e ricarica della whitelist, testo e
riscrittura MIME del banner (box HTML, fallback testo, base64 e multipart),
segnaposto `{days}`/`{last_seen}`, simulazione completa di una transazione
SMTP con un finto modulo
`Milter`, prefisso oggetto, messaggi firmati saltati, fallback su corpo troppo
grande, fallback quando `insheader` non e' disponibile, e il fatto che un
guasto del decider non interrompa mai la posta.

## 13. Licenza

GPL-2.0-or-later. Il testo completo e' in `LICENSE`.
