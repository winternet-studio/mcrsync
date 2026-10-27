#!/bin/sh
# Test SSH host on port 2222 with two users:
#   keyuser - key login only (public key from the shared /keys volume)
#   pwuser  - password login, password "secret"
set -e

if command -v adduser >/dev/null 2>&1 && [ -f /etc/alpine-release ]; then
    adduser -D -s /bin/sh keyuser 2>/dev/null || true
    adduser -D -s /bin/sh pwuser 2>/dev/null || true
else
    id keyuser >/dev/null 2>&1 || useradd -m -s /bin/bash keyuser
    id pwuser >/dev/null 2>&1 || useradd -m -s /bin/bash pwuser
fi
# busybox adduser -D locks the account, which sshd refuses even for keys
echo "keyuser:$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')" | chpasswd
echo "pwuser:secret" | chpasswd

mkdir -p /home/keyuser/.ssh
cp /keys/id_test.pub /home/keyuser/.ssh/authorized_keys
chown -R keyuser:keyuser /home/keyuser/.ssh
chmod 700 /home/keyuser/.ssh
chmod 600 /home/keyuser/.ssh/authorized_keys

ssh-keygen -A >/dev/null
mkdir -p /run/sshd
cat > /etc/ssh/sshd_config.mcrsync <<'EOF'
Port 2222
PasswordAuthentication yes
KbdInteractiveAuthentication no
PermitRootLogin no
Match User keyuser
    PasswordAuthentication no
EOF
exec /usr/sbin/sshd -D -e -f /etc/ssh/sshd_config.mcrsync
