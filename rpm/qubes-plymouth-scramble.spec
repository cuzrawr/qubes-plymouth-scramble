# Qubes 4.3 dom0 uses /usr/sbin and RPM 4 even when the builder uses newer Fedora.
%global _sbindir /usr/sbin
%global _rpmformat 4
# The same source gives the same package: no build host name, times from the changelog below
%global _buildhost localhost
%global source_date_epoch_from_changelog 1
%global use_source_date_epoch_as_buildtime 1
%global clamp_mtime_to_source_date_epoch 1
%global module_dir %{_prefix}/lib/dracut/modules.d/91qubes-scramble
%global theme_state %{_sharedstatedir}/qubes-plymouth-scramble

Name:           qubes-plymouth-scramble
Version:        0.2.0
Release:        1
Summary:        Scrambled digit keys for the Qubes OS disk password prompt
License:        MIT
URL:            https://github.com/cuzrawr/qubes-plymouth-scramble
Source0:        %{url}/archive/v%{version}/%{name}-%{version}.tar.gz
BuildArch:      noarch

Requires:       bash >= 5.1
Requires:       coreutils
Requires:       dracut
Requires:       grep
Requires:       libxkbcommon.so.0()(64bit)
Requires:       plymouth-plugin-script
Requires:       plymouth-scripts
Requires:       python3 >= 3.12
Requires:       qubes-artwork-plymouth
Requires:       sed
Requires:       systemd
Requires:       systemd-udev
Requires:       xkeyboard-config

%description
Randomize the digit keys at the Plymouth disk password prompt on Qubes OS 4.3.
This package installs the dracut hooks and a tool to apply or remove a
completed theme. Generate the theme in a separate VM. The package is inactive
until a theme is applied.

%prep
%setup -q

%install
install -Dpm 0755 qubes-plymouth-scramble %{buildroot}%{_sbindir}/qubes-plymouth-scramble
install -d -m 0755 %{buildroot}%{module_dir} %{buildroot}%{theme_state}
install -pm 0755 dracut/module-setup.sh dracut/qubes-scramble.sh %{buildroot}%{module_dir}/
install -pm 0644 dracut/qubes-scramble.service dracut/qubes-scramble-restore.service %{buildroot}%{module_dir}/

%pre
if [ -e /usr/local/sbin/qubes-plymouth-scramble ] || [ -L /usr/local/sbin/qubes-plymouth-scramble ] ||
   [ -e %{module_dir}/source ] || [ -L %{module_dir}/source ]; then
    echo 'qubes-plymouth-scramble: remove the 0.1.x installation first: sudo qubes-plymouth-scramble uninstall' >&2
    exit 1
fi
if [ "$1" -eq 1 ] && { [ -e %{module_dir} ] || [ -L %{module_dir} ]; }; then
    echo 'qubes-plymouth-scramble: refusing to replace an existing %{module_dir}; inspect and remove it first' >&2
    exit 1
fi

%posttrans
if [ -f %{theme_state}/theme/keypad.script ]; then
    if ! %{_sbindir}/qubes-plymouth-scramble apply --yes; then
        echo 'qubes-plymouth-scramble: automatic rebuild failed; run sudo qubes-plymouth-scramble apply to retry' >&2
    fi
fi
exit 0

%preun
if [ "$1" -eq 0 ]; then
    if ! %{_sbindir}/qubes-plymouth-scramble remove --yes; then
        echo 'qubes-plymouth-scramble: removal failed; run sudo qubes-plymouth-scramble remove to inspect the error' >&2
        exit 1
    fi
fi

%files
%license LICENSE
%doc README.md versions.txt
%{_sbindir}/qubes-plymouth-scramble
%{module_dir}/
%dir %{theme_state}

%changelog
* Tue Nov 10 2026 Package maintainers - 0.2.0-1
- See versions.txt for release notes.
