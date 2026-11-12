%global debug_package %{nil}

# Percorsi systemd. Su EL8 le macro arrivano dal pacchetto systemd, su EL9 ed
# EL10 da systemd-rpm-macros; i fallback coprono build minimali.
%{!?_unitdir:%global _unitdir %{_prefix}/lib/systemd/system}
%{!?_tmpfilesdir:%global _tmpfilesdir %{_prefix}/lib/tmpfiles.d}

Name:           first-contact
Version:        1.0.1
Release:        1%{?dist}
Summary:        Milter Postfix che segnala il primo messaggio da un mittente

License:        GPL-2.0-or-later
URL:            https://github.com/fdefilippo/first-contact
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch
# Fornisce %{python3_sitelib}; le macro systemd sono nel pacchetto systemd
# su EL8 e in systemd-rpm-macros da EL9 in poi.
BuildRequires:  python3-rpm-macros
%if 0%{?rhel} >= 9
BuildRequires:  systemd-rpm-macros
%endif

Requires:       python3
Requires:       python3-pymilter
Requires:       postfix
Requires(pre):  shadow-utils
Requires(post): systemd
Requires(preun): systemd
Requires(postun): systemd

%description
Milter per Postfix che aggiunge un header X-First-Contact e, se previsto,
un avviso visibile (banner nel corpo o prefisso nell'oggetto) quando un
destinatario riceve per la prima volta da un mittente, oppure quando un
mittente noto ricompare dopo un'assenza prolungata.

Il pacchetto installa il comando first-contact, la configurazione in
/etc/first-contact e l'unita' systemd first-contact-milter. Non modifica
la configurazione di Postfix: il milter va aggiunto a smtpd_milters.

%prep
%setup -q

%build

%install
rm -rf %{buildroot}
install -d %{buildroot}%{python3_sitelib}
cp -a src/firstcontact %{buildroot}%{python3_sitelib}/
find %{buildroot}%{python3_sitelib} -name '__pycache__' -prune -exec rm -rf {} +

install -d %{buildroot}%{_bindir}
install -m 0755 packaging/first-contact %{buildroot}%{_bindir}/first-contact

install -d %{buildroot}%{_sysconfdir}/first-contact
install -m 0644 etc/first-contact.ini %{buildroot}%{_sysconfdir}/first-contact/config.ini
install -m 0644 etc/whitelist.txt %{buildroot}%{_sysconfdir}/first-contact/whitelist.txt

install -d %{buildroot}%{_unitdir}
install -m 0644 systemd/first-contact-milter.service %{buildroot}%{_unitdir}/first-contact-milter.service

install -d %{buildroot}%{_tmpfilesdir}
install -m 0644 etc/tmpfiles.d/first-contact.conf %{buildroot}%{_tmpfilesdir}/first-contact.conf

install -d %{buildroot}%{_mandir}/man8
install -m 0644 man/first-contact.8 %{buildroot}%{_mandir}/man8/first-contact.8

%pre
getent group firstcontact >/dev/null || groupadd -r firstcontact
getent passwd firstcontact >/dev/null || \
    useradd -r -g firstcontact -d /var/lib/first-contact -s /sbin/nologin \
        -c "first-contact milter" firstcontact
exit 0

%post
%systemd_post first-contact-milter.service
%tmpfiles_create %{_tmpfilesdir}/first-contact.conf
# abilita e avvia al primo install; sugli aggiornamenti lascia lo stato
if [ $1 -eq 1 ] && [ -d /run/systemd/system ]; then
    systemctl enable --now first-contact-milter.service >/dev/null 2>&1 || :
fi

%preun
%systemd_preun first-contact-milter.service

%postun
%systemd_postun_with_restart first-contact-milter.service

%files
%doc README.md README.it.md examples/sample.eml
%license LICENSE
%{_bindir}/first-contact
%{python3_sitelib}/firstcontact/
%dir %{_sysconfdir}/first-contact
%config(noreplace) %{_sysconfdir}/first-contact/config.ini
%config(noreplace) %{_sysconfdir}/first-contact/whitelist.txt
%{_unitdir}/first-contact-milter.service
%{_tmpfilesdir}/first-contact.conf
%{_mandir}/man8/first-contact.8*

%changelog
* Thu Nov 12 2026 Francesco Defilippo <francesco@defilippo.org> - 1.0.1-1
- Documentazione in inglese e pagina di manuale first-contact(8).

* Thu Nov 12 2026 Francesco Defilippo <francesco@defilippo.org> - 1.0.0-1
- Rilascio iniziale.
