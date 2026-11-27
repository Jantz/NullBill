#!/usr/bin/env bash
# Container check for the desktop role, run by tools/check-roles in a
# Fedora 45 container (tests/image): converge twice, check the sign-in at
# boot, the lock at start, KRDP's settings and the units the role enables,
# then run KRDP as the desktop user with the browser CA's certificate and
# prove with FreeRDP that it presents that certificate, lets the desktop
# user in with the password and no one else: a wrong password, another
# user. A container has no GPU and no compositor, so KRDP stops at opening
# the screen; that part runs on a real desktop (verify).
set -euo pipefail
# shellcheck source=tools/role-check/role-check.sh
source /usr/local/lib/role-check.sh

pw=correct-horse-battery-staple
name=desktop.nullbill.internal
browser_pki /tmp/pki "$name" 127.0.0.1
vars=/tmp/desktop-vars.json
python3 - "$pw" >"$vars" <<'EOF'
import json, sys
print(json.dumps({
    "desktop_user": "owner",
    "desktop_password": sys.argv[1],
    "desktop_gpu_vendor": "nvidia",
    "desktop_cert": open("/tmp/pki/browser.crt").read(),
    "desktop_key": open("/tmp/pki/browser.key").read(),
}))
EOF

converge desktop.yml -e desktop_manage_services=false -e "@$vars"

rpm -q plasma-workspace krdp plasma-login-manager mesa-vulkan-drivers kernel-modules node-exporter dnf5-plugin-automatic >/dev/null ||
  fail "Plasma, KRDP, Plasma Login Manager, Mesa's Vulkan drivers, the kernel's modules, node_exporter and dnf5-automatic"
id -nG owner | tr ' ' '\n' | grep -qx wheel || fail "the desktop user is an administrator"
[ "$(printf '%s' "$pw" | /usr/local/sbin/nb-desktop password owner)" = same ] || fail "the password is the desktop user's"
[ "$(printf 'other-password-123' | /usr/local/sbin/nb-desktop password owner)" = changed ] || fail "another password is set"
[ "$(printf '%s' "$pw" | /usr/local/sbin/nb-desktop password owner)" = changed ] || fail "and back"
pass "the desktop user is an administrator with the password apply gave; nb-desktop sets it only when it differs"

grep -qx "User=owner" /etc/plasmalogin.conf.d/50-nullbill.conf || fail "autologin user"
grep -qx "Session=plasma.desktop" /etc/plasmalogin.conf.d/50-nullbill.conf || fail "autologin session"
[ -f /usr/share/wayland-sessions/plasma.desktop ] || fail "the Plasma session autologin names"
grep -qx "LockOnStart=true" /etc/xdg/kscreenlockerrc || fail "lock at start"
[ -f /etc/plasma-setup-done ] || fail "first-run wizard skipped"
[ "$(readlink /etc/systemd/system/default.target)" = /usr/lib/systemd/system/graphical.target ] || fail "boots to the desktop"
[ "$(readlink /etc/systemd/system/display-manager.service)" = /usr/lib/systemd/system/plasmalogin.service ] || fail "display manager"
[ -e /etc/systemd/user/plasma-workspace.target.wants/app-org.kde.krdpserver.service ] || fail "KRDP starts with the session"
pass "Plasma Login Manager signs the user in at boot, the session locks at start, KRDP starts with it"

grep -qx "apply_updates = yes" /etc/dnf/automatic.conf || fail "dnf5-automatic installs updates"
grep -qx "reboot = never" /etc/dnf/automatic.conf || fail "dnf5-automatic never restarts"
grep -q "textfile.directory=/var/lib/prometheus/node-exporter" /etc/default/prometheus-node-exporter || fail "textfile collector"
grep -q "ExecStart=/usr/bin/prometheus-node-exporter \$ARGS" /usr/lib/systemd/system/node_exporter.service || fail "node_exporter reads ARGS"
{ [ -x /etc/nullbill/busy.d/desktop ] && [ -x /usr/local/sbin/nb-busy ]; } || fail "nb-busy and the desktop's answer"
pass "daily updates install by themselves, node_exporter reads the update metrics, nb-busy asks the desktop"

[ "$(stat -c '%U:%G %a' /etc/nullbill/desktop/krdp.key)" = "root:owner 640" ] || fail "the key is the desktop user's to read only"
grep -qx "ListenPort=3389" /etc/xdg/krdpserverrc || fail "port"
grep -qx "SystemUserEnabled=true" /etc/xdg/krdpserverrc || fail "sign in with the system password"
grep -qx "ExclusiveMode=true" /etc/xdg/krdpserverrc || fail "a screen of the client's own"
grep -qx "AutogenerateCertificates=false" /etc/xdg/krdpserverrc || fail "no certificate of KRDP's own"

# KRDP as the session would start it, without a compositor to stream.
dnf -y -q install freerdp xorg-x11-server-Xvfb dbus-daemon >/dev/null
install -d -o owner -g owner -m 0700 /tmp/xdg-owner
runuser -u owner -- env XDG_RUNTIME_DIR=/tmp/xdg-owner dbus-daemon --session --address=unix:path=/tmp/xdg-owner/bus --fork --nopidfile
(cd /home/owner && runuser -u owner -- env -i HOME=/home/owner USER=owner LANG=C.UTF-8 XDG_RUNTIME_DIR=/tmp/xdg-owner \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/tmp/xdg-owner/bus QT_QPA_PLATFORM=offscreen QT_FORCE_STDERR_LOGGING=1 \
  /usr/bin/krdpserver >/tmp/krdp.log 2>&1 &)
for _ in $(seq 30); do ss -Hltn 'sport = :3389' | grep -q . && break; sleep 1; done
ss -Hltn 'sport = :3389' | grep -q . || { cat /tmp/krdp.log; fail "KRDP listens on 3389"; }
pass "KRDP listens on 3389 with the browser CA's certificate settings"

echo "127.0.0.1 $name" >>/etc/hosts
(setsid Xvfb :9 -screen 0 1024x768x24 >/tmp/xvfb.log 2>&1 &)
cp /tmp/pki/browser-ca.crt /etc/pki/ca-trust/source/anchors/test-browser-ca.crt && update-ca-trust
rdp() { # user password: one connection, KRDP's answer in /tmp/krdp-try.log
  : >/tmp/krdp.log
  env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY DISPLAY=:9 timeout 30 \
    xfreerdp /v:"$name" /u:"$1" /p:"$2" /cert:deny >/tmp/rdp.log 2>&1 || true
  sleep 1
  tr -d '\0' </tmp/krdp.log | grep -av WARN >/tmp/krdp-try.log || true
}
rdp owner "$pw"
grep -q "pam_authenticate failure" /tmp/krdp-try.log && fail "the desktop user's password was refused"
grep -q "New client connected" /tmp/krdp-try.log || { cat /tmp/rdp.log; fail "FreeRDP took the certificate for $name (the browser CA's)"; }
grep -q "Could not open a new remote desktop session" /tmp/krdp-try.log || { cat /tmp/krdp-try.log; fail "KRDP let the desktop user in"; }
pass "FreeRDP trusts the certificate for $name; the desktop user's password gets past sign-in to the screen"

rdp owner wrong-password-1
grep -q "pam_authenticate failure" /tmp/krdp-try.log || { cat /tmp/krdp-try.log; fail "a wrong password was refused"; }
echo "root:$pw" | chpasswd
rdp root "$pw"
grep -q "Could not open a new remote desktop session" /tmp/krdp-try.log && fail "another user got in with a password"
pass "a wrong password and another user are refused"
